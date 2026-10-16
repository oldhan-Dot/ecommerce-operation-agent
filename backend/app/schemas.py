"""身份和店铺接口契约；拒绝额外字段，不回显密码。"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.security import validate_password

EntityId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$")]
RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]

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


class SkuInput(Input):
    id: EntityId | None = None
    code: RequiredText = Field(max_length=64)
    spec_name: RequiredText = Field(max_length=200)
    price: Money
    cost: Money | None = None


class ProductInput(Input):
    store_id: EntityId
    code: RequiredText = Field(max_length=64)
    name: RequiredText = Field(max_length=200)
    category: RequiredText = Field(max_length=128)
    selling_points: RequiredText = Field(max_length=5000)
    brand: str | None = Field(default=None, max_length=100)
    audience: str | None = Field(default=None, max_length=5000)
    source_url: str | None = Field(default=None, max_length=2048)
    skus: list[SkuInput] = Field(min_length=1, max_length=100)

    @field_validator("source_url")
    @classmethod
    def public_url(cls, value):
        """仅保存 HTTP(S) 来源文字，不抓取或执行用户提供的地址。"""
        from urllib.parse import urlsplit

        if not value:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("来源链接必须为不含登录凭据的 HTTP(S) 地址")
        return value

    @field_validator("skus")
    @classmethod
    def unique_skus(cls, values):
        codes = [x.code for x in values]
        ids = [x.id for x in values if x.id is not None]
        if len(set(codes)) != len(codes) or len(set(ids)) != len(ids):
            raise ValueError("SKU 编码和 ID 不得重复")
        return values
