from pydantic import BaseModel, ConfigDict


class RequestModel(BaseModel):
    # Reject unknown fields so typos (or attempts to send e.g. "amount") fail loudly.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
