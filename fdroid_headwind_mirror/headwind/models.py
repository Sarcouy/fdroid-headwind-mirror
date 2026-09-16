from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ResponseStatus(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    ERROR = "ERROR"


class HeadwindModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Application(HeadwindModel):
    id: int
    name: str
    pkg: str
    version: str | None = None
    version_code: int | None = Field(default=None, alias="versionCode")
    url: str | None = None
    latest_version: int | None = Field(default=None, alias="latestVersion")
    type: str | None = None
    split: bool = False
    arch: str | None = None
    customer_id: int | None = Field(default=None, alias="customerId")
    common: bool = False
    system: bool = False


class ApplicationVersion(HeadwindModel):
    id: int
    application_id: int = Field(alias="applicationId")
    version: str | None = None
    version_code: int | None = Field(default=None, alias="versionCode")
    url: str | None = None
    split: bool = False
    url_armeabi: str | None = Field(default=None, alias="urlArmeabi")
    url_arm64: str | None = Field(default=None, alias="urlArm64")


class ApplicationConfigurationLink(HeadwindModel):
    id: int | None = None
    configuration_id: int = Field(alias="configurationId")
    configuration_name: str | None = Field(default=None, alias="configurationName")
    application_id: int | None = Field(default=None, alias="applicationId")
    application_name: str | None = Field(default=None, alias="applicationName")
    action: int | None = None
    remove: bool = False
    show_icon: bool | None = Field(default=None, alias="showIcon")
    outdated: bool | None = None
    current_version_text: str | None = Field(default=None, alias="currentVersionText")
    latest_version_text: str | None = Field(default=None, alias="latestVersionText")

    @property
    def installs_application(self) -> bool:
        return self.action == 1
