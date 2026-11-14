"""扩展策略共用严格模型/日程；不读取配置、数据库或环境。"""
import re
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel,ConfigDict,Field,model_validator


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)


class Schedule(StrictModel):
    frequency:Literal['daily','weekly']
    weekday:int=Field(ge=0,le=6)
    time:str
    timezone:str

    @model_validator(mode='after')
    def valid_clock(self):
        if re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',self.time) is None:
            raise ValueError('invalid clock')
        ZoneInfo(self.timezone)
        return self
