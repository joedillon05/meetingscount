"""
Paying for a self-serve profile. Every screen that touches money is
Stripe's: checkout to pay, the billing portal to change a card or cancel.
This router hands out the links to those pages, records what Stripe says
afterwards, and takes Stripe's webhooks.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import billing
from ..auth import audit, current_user
from ..config import settings
from ..db import get_db
from ..models import Role, User
from ..schemas import CheckoutSyncIn

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _base(request: Request) -> str:
    """Where Stripe sends people back to. The public address, or this request's."""
    return settings.PUBLIC_URL or str(request.base_url).rstrip("/")


def _self_serve(db: Session, user: User):
    if user.role != Role.participant:
        raise HTTPException(403, "Billing is for participants who pay for their own profile.")
    row = billing.row_for(db, user)
    if row is None:
        raise HTTPException(404, "Your county covers MeetingsCount for you. There is nothing to pay.")
    return row


@router.get("")
def my_billing(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return {"subscription": billing.status_out(db, user)}


@router.post("/checkout")
def checkout(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A link to Stripe's checkout page for this person's subscription."""
    row = _self_serve(db, user)
    if not billing.configured():
        raise HTTPException(409, "Payment is not set up on this server yet.")
    if row.active and row.status != "demo":
        raise HTTPException(409, "Your subscription is already active.")
    try:
        url = billing.checkout_url(db, user, _base(request))
    except billing.StripeError as exc:
        raise HTTPException(502, f"Stripe did not open checkout: {exc}") from exc
    audit(db, user, "open_checkout", target_type="user", target_id=user.id, request=request)
    return {"url": url}


@router.post("/sync")
def sync(body: CheckoutSyncIn, request: Request, db: Session = Depends(get_db),
         user: User = Depends(current_user)):
    """Back from checkout: record the result now rather than waiting for the webhook."""
    _self_serve(db, user)
    if not billing.configured():
        raise HTTPException(409, "Payment is not set up on this server yet.")
    try:
        row = billing.sync_checkout(db, user, body.session_id.strip())
    except billing.StripeError as exc:
        raise HTTPException(502, f"Could not confirm the payment with Stripe: {exc}") from exc
    audit(db, user, "checkout_returned", target_type="user", target_id=user.id,
          detail=row.status, request=request)
    return {"subscription": billing.status_out(db, user)}


@router.post("/portal")
def portal(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A link to Stripe's billing portal: card, invoices, cancel."""
    _self_serve(db, user)
    if not billing.configured():
        raise HTTPException(409, "Payment is not set up on this server yet.")
    try:
        url = billing.portal_url(db, user, _base(request))
    except billing.StripeError as exc:
        raise HTTPException(502, f"Stripe did not open the billing portal: {exc}") from exc
    audit(db, user, "open_billing_portal", target_type="user", target_id=user.id, request=request)
    return {"url": url}


@router.post("/webhook", include_in_schema=False)
async def webhook(request: Request, db: Session = Depends(get_db)):
    """
    Stripe calling us. The signature is checked against the endpoint secret
    before the body is even parsed; anything else is refused. Always answers
    quickly, because Stripe retries anything that does not.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "webhook secret is not set")
    payload = await request.body()
    if not billing.signature_ok(payload, request.headers.get("stripe-signature")):
        raise HTTPException(400, "bad signature")
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(400, "not JSON") from exc
    if not isinstance(event, dict) or not event.get("type"):
        raise HTTPException(400, "not an event")
    did = billing.handle_event(db, event)
    county = billing.row_for_event_county(db, event)
    audit(db, None, "billing_event", target_type="stripe", detail=f"{event['type']}: {did}"[:400],
          request=request, county_id=county)
    return {"ok": True, "did": did}
