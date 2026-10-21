from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

CASE_STATUSES = [
    "Needs Review",
    "Ready for Follow-up",
    "Follow-up Approved",
    "Follow-up Simulated",
    "Awaiting Verification",
    "Verified in Demo Data",
    "Closed Without Verification",
]


class ManualInvoiceCreate(BaseModel):
    supplier_name: str
    supplier_gstin: Optional[str] = None
    invoice_number: str
    invoice_date: str
    taxable_value: float
    cgst: float = 0
    sgst: float = 0
    igst: float = 0
    invoice_total: Optional[float] = None
    uncertain: bool = False

    @field_validator("taxable_value", "cgst", "sgst", "igst")
    @classmethod
    def non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("Value cannot be negative")
        return value


class CaseStatusUpdate(BaseModel):
    status: Literal[
        "Needs Review",
        "Ready for Follow-up",
        "Follow-up Approved",
        "Follow-up Simulated",
        "Awaiting Verification",
        "Verified in Demo Data",
        "Closed Without Verification",
    ]
    note: Optional[str] = None


class FollowupApprovalRequest(BaseModel):
    message: str = Field(min_length=10)


class AgentRequest(BaseModel):
    invoice_id: str
    user_request: str = Field(min_length=3)


class LaterStatementVerifyRequest(BaseModel):
    statement_period: Optional[str] = None


class GSTR2BImportRecord(BaseModel):
    supplier_gstin: Optional[str] = None
    invoice_number: str
    invoice_date: Optional[str] = None
    taxable_value: Optional[float] = None
    cgst: float = 0
    sgst: float = 0
    igst: float = 0
    total_gst: float
    statement_period: Optional[str] = None


class AgentToolRequest(BaseModel):
    invoice_id: str


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    data: dict
    error: Optional[str] = None
