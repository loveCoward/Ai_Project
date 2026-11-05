from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from presales.infrastructure.config import OutboundServiceSettings, OutboundSettings


class OutboundService(StrEnum):
    LANGUAGE_MODEL = "language_model"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    JUDGE = "judge"
    OBSERVABILITY = "observability"


class OutboundConfigurationError(ValueError):
    pass


class OutboundNotConfigured(OutboundConfigurationError):
    pass


class OutboundDataNotAllowed(OutboundConfigurationError):
    pass


@dataclass(frozen=True)
class OutboundTarget:
    service: OutboundService
    mode: str
    destination_alias: str | None
    endpoint: str | None
    service_identity: str | None


class OutboundAdapter:
    _OBSERVATION_FIELDS = frozenset({"operation", "status", "duration_ms", "service_mode"})

    def __init__(self, settings: OutboundSettings) -> None:
        self._settings = settings

    def _service_settings(self, service: OutboundService) -> OutboundServiceSettings:
        return getattr(self._settings, service.value)

    def resolve(self, service: OutboundService, data_class: str) -> OutboundTarget:
        configured = self._service_settings(service)
        if configured.mode == "disabled":
            raise OutboundNotConfigured(f"{service.value} outbound service is disabled")
        if data_class not in configured.allowed_data_classes:
            raise OutboundDataNotAllowed(
                f"{data_class} data is not allowed for {service.value} outbound service"
            )
        return OutboundTarget(
            service=service,
            mode=configured.mode,
            destination_alias=configured.destination_alias,
            endpoint=str(configured.endpoint) if configured.endpoint is not None else None,
            service_identity=configured.service_identity,
        )

    def describe(self, service: OutboundService) -> OutboundTarget:
        configured = self._service_settings(service)
        return OutboundTarget(
            service=service,
            mode=configured.mode,
            destination_alias=configured.destination_alias,
            endpoint=str(configured.endpoint) if configured.endpoint is not None else None,
            service_identity=configured.service_identity,
        )

    def observation(
        self, data_class: str, metadata: Mapping[str, str | int]
    ) -> tuple[OutboundTarget, dict[str, str | int]]:
        target = self.resolve(OutboundService.OBSERVABILITY, data_class)
        if metadata.keys() - self._OBSERVATION_FIELDS or any(
            isinstance(value, bool) or not isinstance(value, str | int)
            for value in metadata.values()
        ):
            raise OutboundDataNotAllowed("Observation metadata contains disallowed fields")
        return target, dict(metadata)
