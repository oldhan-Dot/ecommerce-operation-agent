"""其余五类任务的严格输入契约；日期、数量、预算与选定来源在提交时校验。"""

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.daily_data import business_today
from app.schemas import EntityId, Input, Money

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ProductSelection(Input):
    product_ids: list[EntityId] = Field(min_length=1, max_length=5)

    @field_validator("product_ids")
    @classmethod
    def unique_products(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("商品不能重复选择")
        return values


class CompetitorParams(Input):
    product_id: EntityId
    competitor_ids: list[EntityId] = Field(min_length=1, max_length=5)
    dimensions: list[Literal["price", "selling_points", "brand", "spec"]] = Field(
        default_factory=lambda: ["price", "selling_points"], min_length=1, max_length=4
    )

    @field_validator("competitor_ids", "dimensions")
    @classmethod
    def unique_selection(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("选择项不能重复")
        return values


class ThemeParams(ProductSelection):
    goal: Text
    audience: Text
    count: int = Field(default=5, ge=1, le=10, strict=True)
    tone: Label | None = None
    constraints: str | None = Field(default=None, max_length=5000)


class VideoParams(Input):
    product_id: EntityId
    goal: Text
    audience: Text
    duration_seconds: Literal[15, 30, 60]
    theme: Label | None = None
    style: Label | None = None
    forbidden_terms: list[Label] = Field(default_factory=list, max_length=30)


class DateRange(Input):
    start_date: date
    end_date: date

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def iso_date(cls, value):
        if type(value) is not date and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
        ):
            raise ValueError("日期须为YYYY-MM-DD格式")
        return value

    @model_validator(mode="after")
    def valid_period(self):
        length = (self.end_date - self.start_date).days + 1
        if (
            not 1 <= length <= 90
            or self.end_date > business_today()
            or self.start_date.toordinal() <= length
        ):
            raise ValueError("经营日期须不含未来日期且为1–90天")
        return self


class AdParams(DateRange, ProductSelection):
    goal: Text
    budget: Money = Field(gt=0)
    channels: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    ] = Field(min_length=1, max_length=5)
    history_period: DateRange | None = None
    audience: Text | None = None
    target_metrics: list[Label] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def valid_period(self):
        """投放计划允许未来日期，历史区间单独按已发生经营日期约束。"""
        if not 1 <= (self.end_date - self.start_date).days + 1 <= 90:
            raise ValueError("投放计划须为1–90天")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("渠道不能重复")
        return self


class ReviewParams(DateRange):
    goal: Text


class CompetitorTask(Input):
    task_type: Literal["COMPETITOR_ANALYSIS"]
    store_id: EntityId
    input: CompetitorParams


class ThemeTask(Input):
    task_type: Literal["THEME_IDEAS"]
    store_id: EntityId
    input: ThemeParams


class VideoTask(Input):
    task_type: Literal["VIDEO_SCRIPT"]
    store_id: EntityId
    input: VideoParams


class AdTask(Input):
    task_type: Literal["AD_STRATEGY"]
    store_id: EntityId
    input: AdParams


class ReviewTask(Input):
    task_type: Literal["BUSINESS_REVIEW"]
    store_id: EntityId
    input: ReviewParams
