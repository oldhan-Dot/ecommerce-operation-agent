"""身份和店铺接口契约；拒绝额外字段，不回显密码。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.security import validate_password

Role = Literal["ADMIN", "SUPERVISOR", "OPERATOR"]
Status = Literal["ACTIVE", "DISABLED"]
LoginName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    ),
]
Password = Annotated[str, Field(min_length=12, max_length=64)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Login(Input):
    login_name: LoginName
    password: str = Field(min_length=1, max_length=64)


class ChangePassword(Input):
    old_password: str = Field(min_length=1, max_length=64)
    new_password: Password
    _policy = field_validator("new_password")(validate_password)


class CreateUser(Input):
    login_name: LoginName
    password: Password
    role: Role
    _policy = field_validator("password")(validate_password)


class UpdateUser(Input):
    role: Role | None = None
    status: Status | None = None


class ResetPassword(Input):
    new_password: Password
    _policy = field_validator("new_password")(validate_password)


class UpdateGrants(Input):
    store_ids: list[str] = Field(max_length=1000)

    @field_validator("store_ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        if any(not x.isascii() or not x.isdigit() or not 0 < int(x) < 2**64 for x in values):
            raise ValueError("店铺ID必须为有效的正整数字符串")
        return list(dict.fromkeys(values))


class StoreInput(Input):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=64)
    external_id: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("code", "name", "platform")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("必填字段不能全为空白")
        return value
