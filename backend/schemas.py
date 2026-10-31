from typing import Literal, Optional
from pydantic import BaseModel, EmailStr, Field

Segment = Literal['studio', 'clinica', 'lancamento', 'outro']
class Signup(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    company: str = Field(min_length=2, max_length=80)
    segment: Segment = 'outro'
    phone: str = Field(default='', max_length=30)
class Login(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)
class Invite(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    role: Literal['manager', 'agent'] = 'agent'
class MemberUpdate(BaseModel):
    role: Optional[Literal['manager', 'agent']] = None
    active: Optional[bool] = None
class CompanyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    segment: Optional[Segment] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    timezone: Optional[str] = Field(default=None, max_length=50)
class AdminCompanyUpdate(BaseModel):
    status: Optional[Literal['active', 'suspended']] = None
    plan: Optional[Literal['trial', 'oferta', 'completo', 'rede']] = None
    extend_trial_days: Optional[int] = Field(default=None, ge=1, le=60)
    note: Optional[str] = Field(default=None, max_length=500)
