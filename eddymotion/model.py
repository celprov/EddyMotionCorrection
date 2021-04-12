"""A factory class that adapts DIPY's dMRI models."""
import warnings
import concurrent.futures
import asyncio
import nest_asyncio

import numpy as np
from dipy.core.gradients import gradient_table

nest_asyncio.apply()


class ModelFactory:
    """A factory for instantiating diffusion models."""

    @staticmethod
    def init(gtab, model="TensorModel", **kwargs):
        """
        Instatiate a diffusion model.

        Parameters
        ----------
        gtab : :obj:`numpy.ndarray`
            An array representing the gradient table in RAS+B format.
        model : :obj:`str`
            Diffusion model.
            Options: ``"3DShore"``, ``"SFM"``, ``"Tensor"``, ``"S0"``

        Return
        ------
        model : :obj:`~dipy.reconst.ReconstModel`
            An model object compliant with DIPY's interface.

        """
        if model.lower() in ("s0", "b0"):
            return TrivialB0Model(gtab=gtab, S0=kwargs.pop("S0"))

        # Generate a GradientTable object for DIPY
        gtab = _rasb2dipy(gtab)
        param = {}

        if model.lower().startswith("3dshore"):
            from dipy.reconst.shore import ShoreModel as Model

            param = {
                "radial_order": 6,
                "zeta": 700,
                "lambdaN": 1e-8,
                "lambdaL": 1e-8,
            }

        elif model.lower().startswith("sfm"):
            from eddymotion.utils.model import (
                SFM4HMC as Model,
                ExponentialIsotropicModel,
            )

            param = {
                "isotropic": ExponentialIsotropicModel,
            }

        elif model.lower().startswith("tensor"):
            Model = TensorModel

        elif model.lower().startswith("dki"):
            Model = DKIModel

        else:
            raise NotImplementedError(f"Unsupported model <{model}>.")

        param.update(kwargs)
        return Model(gtab, **param)


class TrivialB0Model:
    """
    A trivial model that returns a *b=0* map always.

    Implements the interface of :obj:`dipy.reconst.base.ReconstModel`.
    Instead of inheriting from the abstract base, this implementation
    follows type adaptation principles, as it is easier to maintain
    and to read (see https://www.youtube.com/watch?v=3MNVP9-hglc).

    """

    __slots__ = ("_S0",)

    def __init__(self, gtab, S0=None, **kwargs):
        """Implement object initialization."""
        if S0 is None:
            raise ValueError("S0 must be provided")

        self._S0 = S0

    def fit(self, *args, **kwargs):
        """Do nothing."""

    def predict(self, gradient, **kwargs):
        """Return the *b=0* map."""
        return self._S0


class TensorModel:
    """A wrapper of :obj:`dipy.reconst.dti.TensorModel."""

    __slots__ = (
        "_S0",
        "_mask",
        "_n_threads",
        "_S0_chunks",
        "_mask_chunks",
        "_model_chunks"
    )

    def __init__(self, gtab, S0=None, mask=None, **kwargs):
        """Instantiate the wrapped tensor model."""
        from dipy.reconst.dti import TensorModel as DipyTensorModel

        for k, v in kwargs.items():
            if k == 'n_threads':
                self._n_threads = v
            else:
                self._n_threads = 1

        self._S0 = None
        self._S0_chunks = None
        if S0 is not None:
            self._S0 = np.clip(
                    S0.astype("float32") / S0.max(),
                    a_min=1e-5,
                    a_max=1.0,
            )
            self._S0_chunks = np.split(S0, self._n_threads, axis=2)

        self._mask = None
        self._mask_chunks = None
        if mask is None and S0 is not None:
            self._mask = self._S0 > np.percentile(self._S0, 35)
            self._mask_chunks = np.split(self._mask, self._n_threads, axis=2)

        if self._mask is not None:
            self._S0 = self._S0[self._mask.astype(bool)]
            self._S0_chunks = np.split(self._S0, self._n_threads, axis=2)

        kwargs = {
                k: v
                for k, v in kwargs.items()
                if k
                   in (
                           "min_signal",
                           "return_S0_hat",
                           "fit_method",
                           "weighting",
                           "sigma",
                           "jac",
                   )
        }

        # Create a TensorModel for each chunk
        self._model_chunks = [
            DipyTensorModel(gtab, **kwargs)
            for _ in range(self._n_threads)
        ]

    @staticmethod
    def fit_chunk(model_chunk, data_chunk):
        """Call model's fit."""
        return model_chunk.fit(data_chunk)

    def fit(self, data, **kwargs):
        """Fit the model chunk-by-chunk asynchronously."""
        if self._mask is not None:
            data = data[self._mask, ...]

        # Split data into chunks of group of slices (axis=2)
        data_chunks = np.split(data, self._n_threads, axis=2)

        # Create a limited thread pool.
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._n_threads,
        )

        loop = asyncio.get_event_loop()
        fit_tasks = [
            loop.run_in_executor(
                executor,
                self.fit_chunk,
                self._model_chunks[i],
                data_chunks[i]
            )
            for i in range(self._n_threads)
        ]

        try:
            event_loop = asyncio.get_event_loop()
            self._model_chunks = event_loop.run_until_complete(asyncio.gather(*fit_tasks))
        finally:
            pass

    @staticmethod
    def predict_chunk(model_chunk, S0_chunk, gradient, step=None):
        """Call predict for chunk and return the predicted diffusion signal."""
        return model_chunk.predict(
                _rasb2dipy(gradient),
                S0=S0_chunk,
                step=step,
        )

    def predict(self, gradient, step=None, **kwargs):
        """Predict asynchronously chunk-by-chunk the diffusion signal."""
        # Create a limited thread pool.
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._n_threads,
        )

        loop = asyncio.get_event_loop()
        predict_tasks = [
            loop.run_in_executor(
                executor,
                self.predict_chunk,
                self._model_chunks[i],
                self._S0_chunks[i],
                gradient, step
            )
            for i in range(self._n_threads)
        ]

        try:
            event_loop = asyncio.get_event_loop()
            predicted = event_loop.run_until_complete(
                asyncio.gather(*predict_tasks)
            )
        finally:
            pass

        predicted = np.squeeze(np.concatenate(predicted, axis=2))

        if predicted.ndim == 3:
            return predicted

        retval = np.zeros_like(self._mask, dtype="float32")
        retval[self._mask, ...] = predicted
        return retval


class DKIModel:
    """A wrapper of :obj:`dipy.reconst.dki.DiffusionKurtosisModel."""

    __slots__ = ("_model", "_S0", "_mask")

    def __init__(self, gtab, S0=None, mask=None, **kwargs):
        """Instantiate the wrapped tensor model."""
        from dipy.reconst.dki import DiffusionKurtosisModel

        self._S0 = np.clip(
            S0.astype("float32") / S0.max(),
            a_min=1e-5,
            a_max=1.0,
        )
        self._mask = mask
        kwargs = {
            k: v
            for k, v in kwargs.items()
            if k
            in (
                "min_signal",
                "return_S0_hat",
                "fit_method",
                "weighting",
                "sigma",
                "jac",
            )
        }
        self._model = DiffusionKurtosisModel(gtab, **kwargs)

    def fit(self, data, **kwargs):
        """Clean-up permitted args and kwargs, and call model's fit."""
        self._model = self._model.fit(data, mask=self._mask)

    def predict(self, gradient, step=None, **kwargs):
        """Propagate model parameters and call predict."""
        return self._model.predict(
            _rasb2dipy(gradient),
            S0=self._S0,
            step=step,
        )


def _rasb2dipy(gradient):
    gradient = np.asanyarray(gradient)
    if gradient.ndim == 1:
        if gradient.size != 4:
            raise ValueError("Missing gradient information.")
        gradient = gradient[..., np.newaxis]

    if gradient.shape[0] != 4:
        gradient = gradient.T
    elif gradient.shape == (4, 4):
        print("Warning: make sure gradient information is not transposed!")

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        retval = gradient_table(gradient[3, :], gradient[:3, :].T)
    return retval
