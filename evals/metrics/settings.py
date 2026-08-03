"""프로세스 환경과 dotenv에서 격리한 평가 전용 설정."""

from app.core.config import Settings


class EvaluationSettings(Settings):
    """init kwargs와 코드 기본값만 사용하는 결정론적 평가 설정."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        del settings_cls, env_settings, dotenv_settings, file_secret_settings
        return (init_settings,)
