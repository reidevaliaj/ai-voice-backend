from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from db import get_db
from services.tenants import build_runtime_context, get_tenant_by_id
from tools.email_resend import send_email_resend

router = APIRouter()


class SendEmailReq(BaseModel):
    tenant_id: str | None = None
    to: EmailStr
    subject: str
    html: str


@router.post("/tools/send-email")
async def tools_send_email(req: SendEmailReq, db: Session = Depends(get_db)):
    try:
        from_email = "Code Studio <noreply@code-studio.eu>"
        reply_to = "Rej Aliaj <info@code-studio.eu>"
        if req.tenant_id:
            tenant = get_tenant_by_id(db, req.tenant_id)
            if tenant is not None:
                runtime = build_runtime_context(db, tenant)
                from_email = str(runtime["integrations"]["email"].get("settings", {}).get("from_email") or runtime["config"].get("from_email") or from_email)
                reply_to = str(runtime["integrations"]["email"].get("settings", {}).get("reply_to_email") or runtime["config"].get("reply_to_email") or reply_to)
        resp = send_email_resend(
            to=req.to,
            subject=req.subject,
            html=req.html,
            from_email=from_email,
            reply_to=reply_to,
        )
        return {"ok": True, "resend": resp}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
