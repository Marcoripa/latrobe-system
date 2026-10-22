from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectStatus = Literal["active", "ready_to_bill", "closed"]


class ProjectBase(BaseModel):
    """Shared attributes for a LaTrobe Irrigation project."""

    model_config = ConfigDict(populate_by_name=True)

    client_name: str = Field(..., min_length=1, description="Customer or farm name")
    qbo_customer_id: str | None = Field(
        default=None, description="QuickBooks Online Customer ID (CustomerRef.value)"
    )
    name: str = Field(..., min_length=1, description="Project title or site name")
    status: ProjectStatus = Field(
        default="active",
        description="Project lifecycle status: active, ready_to_bill, or closed",
    )
    ute_id: str = Field(
        default="ute-01",
        min_length=1,
        description="Assigned service vehicle / Ute identifier",
    )
    hourly_rate: float = Field(
        default=125.0, ge=0, description="Default hourly labor rate for billing"
    )


class ProjectCreate(ProjectBase):
    """Payload for creating a new project."""

    pass


class ProjectUpdate(BaseModel):
    """Payload for partially updating a project."""

    client_name: str | None = None
    qbo_customer_id: str | None = None
    name: str | None = None
    status: ProjectStatus | None = None
    ute_id: str | None = None
    hourly_rate: float | None = Field(default=None, ge=0)


class Project(ProjectBase):
    """Complete project model stored in Firestore."""

    id: str = Field(..., description="Firestore project document ID")
    created_at: datetime | None = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Project creation timestamp",
    )
    updated_at: datetime | None = Field(
        default=None, description="Last modification timestamp"
    )
