"""LinkedIn outreach mixin: send messages and connection invitations.

Anchors on stable aria-labels / visible button text matched across FR/EN locales
(LinkedIn's CSS classes are hashed and change often). Two primitives:

- send_message: only works for 1st-degree connections (or InMail with a premium
  account). For a cold contact, the "Message" button is absent → raises.
- send_invitation: the cold-outreach primitive — connection request, optional note.

Both support dry_run: perform every step except the final send click, and save a
screenshot, so the flow can be validated without contacting a real person.
"""

import random
import re
from pathlib import Path
from typing import Optional

from ._js import JS_MARK_CONTROL, JS_CLEAR_MARK

# Visible-text / aria-label vocabulary (FR + EN)
_MESSAGE_LABELS = ["message"]
_CONNECT_LABELS = ["se connecter", "connect", "inviter", "invite"]
_MORE_LABELS = ["plus", "more"]
_ADD_NOTE_LABELS = ["ajouter une note", "add a note"]
_SEND_LABELS = ["envoyer", "send"]
_SEND_NO_NOTE_LABELS = ["envoyer sans note", "send without a note"]

# Stable-ish LinkedIn DOM anchors that long predate the SDUI rewrite
_COMPOSE_BOX = 'div.msg-form__contenteditable[contenteditable="true"]'
_MSG_SEND_BUTTON = "button.msg-form__send-button"
_INVITE_NOTE_TEXTAREA = "textarea[name='message'], #custom-message"
# LinkedIn's invite modal is an `.artdeco-modal`. Anchor on that class only:
# a bare `div[role='dialog']` also matches hidden video.js error modals
# (`.vjs-error-display`) injected on profiles with featured video, which made
# the connect flow wait forever on the wrong (hidden) element.
_MODAL = ".artdeco-modal[role='dialog'], .artdeco-modal"

_NOTE_MAX = 300  # LinkedIn caps connection notes (200 free / 300 premium)


class OutreachMixin:
    """Send LinkedIn messages and connection invitations via browser automation."""

    # --- low-level control helpers ---

    async def _click_control(self, labels, tags=("button", "a"), timeout: float = 8.0) -> str:
        """Find a visible control matching any label, real-click it. Raise if none."""
        deadline = timeout
        marker = None
        while deadline > 0:
            marker = await self.page.evaluate(
                JS_MARK_CONTROL, {"labels": list(labels), "tags": list(tags)}
            )
            if marker:
                break
            await self.wait(0.5)
            deadline -= 0.5
        if not marker:
            raise RuntimeError(
                f"No clickable control matching {labels} found on the page."
            )
        # JS click rather than a Playwright pointer click: LinkedIn buttons wrap
        # an inner <svg> that intercepts pointer events, which makes actionability
        # clicks retry until timeout. el.click() still fires React's handlers.
        await self.page.eval_on_selector(marker, "el => el.click()")
        await self.page.evaluate(JS_CLEAR_MARK)
        await self.wait(random.uniform(0.8, 1.6))
        return marker

    async def _human_type(self, text: str):
        await self.page.keyboard.type(text, delay=random.randint(25, 70))

    _CONNECT_RE = re.compile(r"se connecter|^\s*connect\b", re.I)
    # The invite modal is identified by its action buttons, not a CSS class
    # (LinkedIn's hashed classes drift; the visible text is stable FR/EN).
    # NB: visibility is tested with getBoundingClientRect, NOT offsetParent —
    # LinkedIn modals are `position: fixed`, so offsetParent is null for their
    # buttons even when fully visible (this false-negative caused spurious
    # "modal never opened" retries).
    _MODAL_PROBE = (
        "() => [...document.querySelectorAll('button')].some(b => {"
        " const r = b.getBoundingClientRect();"
        " return r.width > 0 && r.height > 0 && /ajouter une note|add a note|"
        "envoyer sans note|send without/i.test("
        "(b.innerText || b.getAttribute('aria-label') || '')); })"
    )

    async def _invite_modal_visible(self, timeout: float = 5.0) -> bool:
        deadline = timeout
        while deadline > 0:
            if await self.page.evaluate(self._MODAL_PROBE):
                return True
            await self.wait(0.4)
            deadline -= 0.4
        return False

    async def _real_click_button(self, pattern: str, timeout: int = 6000) -> None:
        """Real Playwright click on a <button> whose text/label matches pattern."""
        rx = re.compile(pattern, re.I)
        loc = self.page.locator("button").filter(has_text=rx)
        await loc.first.click(timeout=timeout)

    async def _open_invite_modal(self) -> None:
        """Open the connection-invite modal, robustly.

        "Connect" is a primary button on some profiles, hidden under "More" on
        others (people who set "Follow" as primary). LinkedIn renders the
        More-menu entry as `<a role="menuitem">`, whose React handler only fires
        on a REAL pointer click — a JS `el.click()` (what `_click_control` does)
        is silently ignored. Hence the Playwright `locator.click()` here, wrapped
        in a retry because the dropdown can race-close.
        """
        for attempt in range(3):
            # direct primary "Connect" button on the top card (aria-label
            # "Inviter <name> à se connecter"); only worth trying once.
            if attempt == 0:
                direct = self.page.locator('main button[aria-label*="se connecter" i]')
                if await direct.count():
                    try:
                        await direct.first.click(timeout=4000)
                        if await self._invite_modal_visible():
                            return
                    except Exception:
                        pass
            # Open the TOP-CARD "More" menu with a REAL Playwright click. The
            # page has many "Plus"/"More" buttons (one per feed post); the first
            # in <main> is the profile-actions overflow. A JS .click() opens the
            # menu but leaves the subsequent menuitem handler flaky, so use a
            # real pointer click here too.
            try:
                more = self.page.locator(
                    'main button[aria-label="Plus"], main button[aria-label="More"]'
                )
                await more.first.click(timeout=5000)
            except Exception:
                pass
            await self.wait(random.uniform(1.4, 2.2))
            try:
                item = self.page.locator('a[role="menuitem"]', has_text=self._CONNECT_RE)
                await item.first.click(timeout=6000)
            except Exception:
                pass
            if await self._invite_modal_visible():
                return
            await self.wait(random.uniform(1.0, 2.0))
        raise RuntimeError("Could not open the invitation modal after 3 attempts.")

    # --- messaging (voyager API — deterministic, no DOM clicking) ---

    # Sends via LinkedIn's internal voyager API from INSIDE the authenticated
    # page (session cookies + CSRF apply). Resolves sender + recipient member
    # URNs, then POSTs createMessage. Endpoint/payload reverse-engineered from a
    # real send (HAR). For non-connections this is a Premium InMail; it works as
    # long as the account is allowed to message the recipient. The DOM flow it
    # replaces broke on every LinkedIn UI drift (stale selectors, fixed-position
    # compose box, wrong "Message" button among sidebar suggestions).
    _SEND_JS = r"""
    async ({text, slug, dry}) => {
      const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1];
      if (!csrf) return {error: 'no_csrf'};
      const hdr = {'csrf-token': csrf, 'accept': 'application/json',
                   'x-restli-protocol-version': '2.0.0'};
      const urn = (t) => (t.match(/urn:li:fsd_profile:ACoAA[\w-]+/) || [])[0] || null;
      let me, rec;
      try { me = urn(await (await fetch('/voyager/api/me', {headers: hdr})).text()); }
      catch (e) { return {error: 'me_fetch: ' + e}; }
      try {
        const r = await fetch('/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity='
                              + encodeURIComponent(slug), {headers: hdr});
        rec = urn(await r.text());
      } catch (e) { return {error: 'recipient_fetch: ' + e}; }
      if (!me || !rec) return {error: 'urn_resolution', me, rec};
      if (dry) return {dry: true, me, rec};
      const tid = Array.from({length: 16},
        () => String.fromCharCode(33 + Math.floor(Math.random() * 94))).join('');
      const payload = {
        message: {body: {attributes: [], text}, originToken: crypto.randomUUID(),
                  renderContentUnions: []},
        mailboxUrn: me, trackingId: tid, dedupeByClientGeneratedToken: false,
        hostRecipientUrns: [rec],
        hostMessageCreateContent: {
          'com.linkedin.voyager.dash.messaging.MessageCreateContent':
            {messageCreateContentUnion: {premiumInMail: {}}}}
      };
      const resp = await fetch(
        '/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage',
        {method: 'POST', headers: {...hdr, 'content-type': 'text/plain;charset=UTF-8'},
         body: JSON.stringify(payload)});
      return {status: resp.status, ok: resp.ok, me, rec, resp: (await resp.text()).slice(0, 300)};
    }
    """

    async def send_message(self, profile_url: str, message: str, dry_run: bool = False) -> dict:
        """Send a message (Premium InMail for non-connections) via the voyager API."""
        if not message.strip():
            raise ValueError("Message body is empty.")
        slug = profile_url.rstrip("/").split("/in/")[-1].split("/")[0].split("?")[0]
        if not slug:
            raise ValueError(f"Could not extract a profile slug from {profile_url!r}")

        await self.check_rate_limit("message")
        await self.goto(profile_url)
        await self._raise_if_auth_wall()
        await self.wait(random.uniform(1.5, 2.5))

        res = await self.page.evaluate(self._SEND_JS,
                                       {"text": message, "slug": slug, "dry": dry_run})
        if res.get("error"):
            raise RuntimeError(f"send_message failed ({res.get('error')}): {res}")
        if dry_run:
            return {"status": "dry_run", "action": "message", "profile": profile_url,
                    "message": message, "sender_urn": res.get("me"),
                    "recipient_urn": res.get("rec")}
        if not res.get("ok"):
            raise RuntimeError(f"createMessage HTTP {res.get('status')}: {res.get('resp')}")
        return {"status": "sent", "action": "message", "profile": profile_url,
                "recipient_urn": res.get("rec")}

    # --- connection invitation (cold outreach) ---

    async def send_invitation(self, profile_url: str, note: Optional[str] = None,
                              dry_run: bool = False) -> dict:
        """Send a connection request, optionally with a note (<=300 chars)."""
        if note and len(note) > _NOTE_MAX:
            raise ValueError(
                f"Note is {len(note)} chars; LinkedIn caps invitation notes at {_NOTE_MAX}."
            )

        await self.check_rate_limit("invitation")
        await self.goto(profile_url)
        await self._raise_if_auth_wall()
        await self.wait(random.uniform(2.0, 3.5))

        await self._open_invite_modal()  # raises if the modal never opens
        await self.wait(random.uniform(0.6, 1.2))

        if note:
            await self._real_click_button(r"ajouter une note|add a note")
            textarea = await self.page.wait_for_selector(_INVITE_NOTE_TEXTAREA, timeout=8000)
            await textarea.click()
            await self._human_type(note)
            await self.wait(random.uniform(0.5, 1.0))

        if dry_run:
            shot = str(Path("/tmp") / "oto-linkedin-invitation.png")
            await self.screenshot(shot, full_page=False)
            await self.page.keyboard.press("Escape")
            return {
                "status": "dry_run",
                "action": "invitation",
                "profile": profile_url,
                "note": note,
                "screenshot": shot,
            }

        # With a note, the modal's primary button is "Envoyer"; without, the
        # first dialog offers "Envoyer sans note".
        send_pattern = r"^envoyer$|^send$|^envoyer l.invitation$" if note \
            else r"envoyer sans note|send without|^envoyer$|^send$"
        await self._real_click_button(send_pattern)
        await self.wait(random.uniform(1.5, 2.5))

        return {"status": "sent", "action": "invitation", "profile": profile_url, "note": note}
