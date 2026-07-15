"""Native ChestMNIST PyTorch inference service.

The engine is imported lazily so shared provisioning helpers can be reused by
the ZK service and the lightweight agent without importing PyTorch.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import ChestMnistTorchEngine

__all__ = ["ChestMnistTorchEngine"]


def __getattr__(name: str) -> Any:
    if name == "ChestMnistTorchEngine":
        from .engine import ChestMnistTorchEngine

        return ChestMnistTorchEngine
    raise AttributeError(name)
