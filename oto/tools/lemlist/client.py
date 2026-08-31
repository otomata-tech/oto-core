"""
Lemlist API Client for email campaign management.

Requires: requests
"""

import json
import re
import time
import base64
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


@dataclass
class Campaign:
    """Campaign data.

    `senders` is kept first-class because it is what an operator asks for
    ("qui envoie cette campagne ?"), but the v2 sample payload of
    `GET /campaigns` does NOT advertise it — on that shape it lands `[]`.
    ⚠️ Not replayed live; confirm with a real key before relying on it.
    """
    id: str
    name: str
    status: str
    senders: List[str]
    emoji: str = ""
    labels: List[str] = field(default_factory=list)
    timezone: str = ""
    created_at: str = ""
    created_by: str = ""
    has_error: bool = False
    errors: List[str] = field(default_factory=list)


@dataclass
class Lead:
    """Lead data for campaign. Uses camelCase to match Lemlist API."""
    email: str
    firstName: str = None
    lastName: str = None
    companyName: str = None
    phone: str = None
    picture: str = None
    linkedinUrl: str = None


class LemlistClient:
    """
    Lemlist API client for:
    - Campaign management (list, get, create, update, start, pause, duplicate,
      statutes)
    - Lead management (add, create, launch, delete, export, enrich)
    - Sequence/step management (get, add, update, delete) and A/B tests
    - Schedules — sending windows, team-owned, shared by campaigns
    - Campaign tree (structured view with branches)
    - Activities & stats (`get_campaign_stats_v2`, batch, reports)

    Two calls, and only two, put messages on the wire: `start_campaign` and
    `launch_lead`. Everything else here edits a draft, reads, or enriches.
    """

    BASE_URL = "https://api.lemlist.com/api"

    def __init__(self, api_key: str = None):
        """
        Initialize Lemlist client.

        Args:
            api_key: Lemlist API key (or set LEMLIST_API_KEY env var)
        """
        self.api_key = api_key or require_secret("LEMLIST_API_KEY")
        self._last_request = 0.0

    @property
    def headers(self) -> dict:
        """Get auth headers dict."""
        return {
            "Authorization": self._get_auth_header(),
            "Content-Type": "application/json",
        }

    def _rate_limit(self):
        """Enforce minimum 100ms between requests."""
        elapsed = time.time() - self._last_request
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)
        self._last_request = time.time()

    def _get_auth_header(self) -> str:
        """Get Basic auth header."""
        credentials = f":{self.api_key}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def _request(self, method: str, endpoint: str, *, tolerate: tuple = (), **kwargs) -> Any:
        """Make API request with rate limiting.

        `tolerate` lists status codes whose body is a legitimate payload rather
        than an error — used by the enrichment poll, where lemlist answers 404
        with `{"enrichmentStatus": "not-found", ...}`.
        """
        self._rate_limit()

        url = f"{self.BASE_URL}/{endpoint}"
        headers = {"Authorization": self._get_auth_header()}

        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        response = requests.request(method, url, headers=headers, **kwargs)
        if response.status_code not in tolerate:
            raise_for_upstream(response, service="lemlist")

        if response.content:
            return response.json()
        return {}

    @staticmethod
    def _flag_params(**flags: bool) -> Dict[str, str]:
        """Lemlist reads boolean flags as query strings — `True` would serialize
        as `"True"`, so only the enabled ones are sent, as `"true"`."""
        return {k: "true" for k, v in flags.items() if v}

    # --- Campaigns ---
    #
    # `GET /campaigns` has two shapes and the doc carries an explicit warning to
    # ask for the recent one: we always send `version=v2` rather than trust the
    # server default. It is ALSO paginated (limit max 100) — `list_campaigns`
    # returns ONE page, `list_all_campaigns` walks them.

    #: Campaign statuses accepted as a `status=` filter. A campaign can hold
    #: several at once (a paused campaign WITH errors), so this filters, it does
    #: not partition.
    CAMPAIGN_STATUSES = ("running", "draft", "archived", "ended", "paused", "errors")

    #: Max page size accepted by `GET /campaigns` and `GET /schedules`.
    PAGE_MAX = 100

    @staticmethod
    def _campaign(c: dict) -> Campaign:
        return Campaign(
            id=c["_id"],
            name=c.get("name", ""),
            status=c.get("status", ""),
            senders=c.get("senders", []),
            emoji=c.get("emoji", ""),
            labels=c.get("labels", []) or [],
            timezone=c.get("timezone", ""),
            created_at=c.get("createdAt", ""),
            created_by=c.get("createdBy", ""),
            has_error=bool(c.get("hasError", False)),
            errors=c.get("errors", []) or [],
        )

    def list_campaigns(
        self,
        *,
        limit: int = None,
        offset: int = None,
        page: int = None,
        status: str = None,
        created_by: str = None,
        sort_order: str = None,
    ) -> List[Campaign]:
        """List ONE page of campaigns (up to 100 — the API maximum).

        Args:
            limit: Page size, 1..100. Default (server-side) 100.
            offset: Records to skip. Used when `page` is not given.
            page: 1-based page number.
            status: One of `CAMPAIGN_STATUSES`.
            created_by: Filter on a creator user id (`usr_…`).
            sort_order: `asc` (default) or `desc`, on `createdAt`.

        A caller that needs the WHOLE workspace must use `list_all_campaigns` —
        this one truncates at the page size, and says nothing about it.
        """
        if status is not None and status not in self.CAMPAIGN_STATUSES:
            raise ValueError(
                f"status must be one of {self.CAMPAIGN_STATUSES}, got {status!r}")
        params = {"version": "v2"}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if page is not None:
            params["page"] = page
        if status is not None:
            params["status"] = status
        if created_by is not None:
            params["createdBy"] = created_by
        if sort_order is not None:
            params["sortBy"] = "createdAt"
            params["sortOrder"] = sort_order
        data = self._request("GET", "campaigns", params=params)
        return [self._campaign(c) for c in data]

    def list_all_campaigns(
        self, *, max_pages: int = 20, **filters,
    ) -> tuple[List[Campaign], bool]:
        """Walk `GET /campaigns` page by page.

        Returns `(campaigns, truncated)` — `truncated` is True when `max_pages`
        was reached with a full page still coming, i.e. the list is INCOMPLETE.
        Returning the flag rather than silently stopping is the whole point: a
        capped list that looks complete is how "cette campagne n'existe pas"
        gets answered wrongly.
        """
        out: List[Campaign] = []
        offset = 0
        for _ in range(max_pages):
            batch = self.list_campaigns(limit=self.PAGE_MAX, offset=offset, **filters)
            out.extend(batch)
            if len(batch) < self.PAGE_MAX:
                return out, False
            offset += self.PAGE_MAX
        return out, True

    def get_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Get campaign details."""
        return self._request("GET", f"campaigns/{campaign_id}")

    def create_campaign(
        self,
        name: str,
        *,
        timezone: str = None,
        auto_review: bool = None,
        auto_review_conditions: List[str] = None,
    ) -> Dict[str, Any]:
        """Create a campaign — empty sequence + default schedule come with it.

        Args:
            name: Campaign name (required by the API).
            timezone: IANA zone for the auto-created schedule (e.g.
                `America/New_York`). Defaults server-side to `Europe/Paris`.
            auto_review: Launch every lead as soon as it is added, instead of
                leaving it paused for manual review. ⚠️ This is the switch that
                turns "add a lead" into "send to a real person" — see
                `AUTO_REVIEW_CONDITIONS` and the caller's own gating.
            auto_review_conditions: Restrict auto-launch to leads whose email
                verification matches these statuses.

        Returns the campaign, including `sequenceId` and `scheduleIds` — the two
        ids every other management call needs.
        """
        body: Dict[str, Any] = {"name": name}
        if timezone is not None:
            body["timezone"] = timezone
        if auto_review is not None:
            body["autoReview"] = auto_review
        if auto_review_conditions is not None:
            self._check_auto_review_conditions(auto_review_conditions)
            body["autoReviewConditions"] = auto_review_conditions
        return self._request("POST", "campaigns", json=body)

    #: Deliverability statuses accepted by `autoReviewConditions`. Checked here
    #: because lemlist answers an invalid one with a bare 400.
    AUTO_REVIEW_CONDITIONS = ("deliverable", "risky", "undeliverable", "unverified")

    @classmethod
    def _check_auto_review_conditions(cls, conditions: List[str]) -> None:
        bad = [c for c in conditions if c not in cls.AUTO_REVIEW_CONDITIONS]
        if bad:
            raise ValueError(
                f"unknown autoReviewConditions {bad} — "
                f"expected any of {cls.AUTO_REVIEW_CONDITIONS}")

    def start_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Start (or resume) a campaign — a no-op if it is already running.

        ⚠️ This is the call that puts real messages on the wire: from here
        lemlist walks the sequence for every launched lead. Nothing else in this
        client, `launch_lead` aside, has that effect.
        """
        return self._request("POST", f"campaigns/{campaign_id}/start")

    def pause_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Pause a campaign — a no-op if it is not running.

        Already-scheduled leads are untouched (lemlist's own wording): pausing
        stops the campaign advancing, it does not recall what is queued.
        """
        return self._request("POST", f"campaigns/{campaign_id}/pause")

    def duplicate_campaign(self, campaign_id: str, name: str = None) -> Dict[str, Any]:
        """Copy a campaign — sequence steps, schedules and AI templates included.

        The copy lands in DRAFT with lead counts at zero (CRM settings are not
        copied). Without `name` lemlist appends " Copy" to the original.
        """
        body = {"name": name} if name is not None else {}
        return self._request("POST", f"campaigns/{campaign_id}/duplicate", json=body)

    def update_campaign(self, campaign_id: str, data: dict) -> Dict[str, Any]:
        """Update campaign settings (PATCH — only the keys you send change).

        Notable keys: `name`, `sendUserIds` (senders, `usr_…`), the stop rules
        (`stopOnEmailReplied`, `stopOnMeetingBooked`, `stopOnLinkClicked`),
        tracking toggles, `aiFeatures`, `onReplied`, and `autoReview` /
        `autoReviewConditions`.
        """
        if "autoReviewConditions" in data:
            self._check_auto_review_conditions(data["autoReviewConditions"])
        return self._request("PATCH", f"campaigns/{campaign_id}", json=data)

    def get_campaign_statutes(self, campaign_id: str) -> Dict[str, Any]:
        """Validation statutes — the same engine the lemlist UI runs.

        Returns `{name, status, statutes: [{type, level, message, category}]}`
        where `level` 3 BLOCKS a launch (no sender, broken DNS…), 2 is an
        actionable warning (daily limit, missing schedule) and 1 is info. This
        is what to read BEFORE `start_campaign`, rather than starting and
        discovering the campaign errors out.
        """
        return self._request("GET", f"campaigns/{campaign_id}/statutes")

    # --- Sequences & Steps ---

    def get_sequences(self, campaign_id: str) -> Dict[str, Any]:
        """Get sequences for a campaign.

        Returns:
            Dict mapping sequence_id -> sequence data with steps and level.
        """
        return self._request("GET", f"campaigns/{campaign_id}/sequences")

    def get_sequence_steps(self, campaign_id: str, sequence_id: str) -> List[Dict]:
        """Get steps for a specific sequence."""
        sequences = self.get_sequences(campaign_id)
        if sequence_id in sequences:
            return sequences[sequence_id].get('steps', [])
        return []

    #: Step types accepted by `POST/PATCH /sequences/{id}/steps`. Checked
    #: locally: a typo lands as a bare 400 that says nothing about the vocabulary.
    STEP_TYPES = (
        "email", "manual", "phone", "api",
        "linkedinVisit", "linkedinInvite", "linkedinSend", "linkedinVoiceNote",
        "linkedinFollow", "linkedinLikeLastPost", "linkedinCommentLastPost",
        "linkedinEndorse", "linkedinWithdrawInvitation",
        "sendToAnotherCampaign", "conditional", "whatsappMessage", "sms",
    )

    #: Condition keys for a `conditional` step (the branch test).
    CONDITION_KEYS = (
        "hasEmailAddress", "hasLinkedinUrl", "hasPhoneNumber", "hasScore",
        "emailsOpened", "emailsClicked", "emailsUnsubscribed", "meetingBooked",
        "linkedinInviteAccepted", "linkedinOpened", "aircallDone",
        "linkedinNetworkCheck", "hasWhatsappAccount",
    )

    def add_step(self, sequence_id: str, step: dict) -> Dict[str, Any]:
        """Add a step (or a condition) to a sequence.

        Args:
            sequence_id: Sequence ID (e.g., 'seq_abc123')
            step: Step data. `type` is REQUIRED and must be one of
                  `STEP_TYPES`. Common fields: `index` (insert position, ≥ -1,
                  appended when omitted or past the end), `delay` (days before
                  the step — server default 0 for the first, 1 after),
                  `subject` (email), `message` (email/linkedin/manual/phone/
                  whatsapp/sms), `title` (manual), `method` + `url` (api),
                  `conditionKey` + `delayType` (conditional), `campaignId`
                  (sendToAnotherCampaign), `images`/`videos` (linkedinInvite /
                  linkedinSend, public HTTPS URLs lemlist re-hosts).
                  For email: {'type': 'email', 'subject': '...', 'message': '...', 'delay': 0}
        """
        self._check_step(step, require_type=True)
        return self._request("POST", f"sequences/{sequence_id}/steps", json=step)

    @classmethod
    def _check_step(cls, step: dict, *, require_type: bool) -> None:
        """Reject a step whose `type`/`conditionKey` is outside the vocabulary."""
        step_type = step.get("type")
        if step_type is None:
            if require_type:
                raise ValueError(
                    f"step needs a 'type' — one of {cls.STEP_TYPES}")
        elif step_type not in cls.STEP_TYPES:
            raise ValueError(
                f"unknown step type {step_type!r} — expected one of {cls.STEP_TYPES}")
        key = step.get("conditionKey")
        if key is not None and key not in cls.CONDITION_KEYS:
            raise ValueError(
                f"unknown conditionKey {key!r} — expected one of {cls.CONDITION_KEYS}")

    def update_step(self, sequence_id: str, step_id: str, data: dict) -> Dict[str, Any]:
        """Update a step.

        Args:
            sequence_id: Sequence ID
            step_id: Step ID
            data: Update data. `type` is REQUIRED by the API and must MATCH the
                existing step — it identifies the step's shape, it does not
                convert it. Same field vocabulary as `add_step`; `images` /
                `videos` REPLACE what the step carries (`[]` clears them).
        """
        self._check_step(data, require_type=True)
        return self._request("PATCH", f"sequences/{sequence_id}/steps/{step_id}", json=data)

    def delete_step(self, sequence_id: str, step_id: str) -> Dict[str, Any]:
        """Delete a step from a sequence.

        Refused by lemlist (400) while the campaign is RUNNING — pause it first.
        """
        return self._request("DELETE", f"sequences/{sequence_id}/steps/{step_id}")

    # --- A/B tests (email steps, Email Pro plan) ---
    #
    # An A/B test is variant B hanging off an email step: creating it starts the
    # split, deleting it ends the test, and picking a winner applies one template
    # to every remaining lead. All four live on the SAME path, one verb each.

    def create_ab_variant(self, sequence_id: str, step_id: str) -> Dict[str, Any]:
        """Create variant B on an email step, prefilled from A, and START the
        split (leads are shared between A and B from here on)."""
        return self._request(
            "POST", f"sequences/{sequence_id}/steps/{step_id}/ab-test")

    def get_ab_variant(self, sequence_id: str, step_id: str) -> Dict[str, Any]:
        """Read variant B (subject, message, config) of an email step's A/B test."""
        return self._request(
            "GET", f"sequences/{sequence_id}/steps/{step_id}/ab-test")

    def update_ab_variant(
        self, sequence_id: str, step_id: str, data: dict,
    ) -> Dict[str, Any]:
        """Edit variant B — only the keys sent change.

        Accepts `subject` (≤ 400 chars), `message` (HTML), `altMessage`, `cc`,
        `plainText`.
        """
        return self._request(
            "PATCH", f"sequences/{sequence_id}/steps/{step_id}/ab-test", json=data)

    def delete_ab_variant(
        self, sequence_id: str, step_id: str, variant: str = "B",
    ) -> Dict[str, Any]:
        """End the A/B test by dropping one variant.

        `variant="B"` (default) drops B; `variant="A"` PROMOTES B to A — the
        step keeps B's content. Sent as a query parameter, not a body.
        """
        if variant not in ("A", "B"):
            raise ValueError(f"variant must be 'A' or 'B', got {variant!r}")
        return self._request(
            "DELETE", f"sequences/{sequence_id}/steps/{step_id}/ab-test",
            params={"variant": variant})

    def select_ab_winner(
        self, sequence_id: str, step_id: str, variant: str,
    ) -> Dict[str, Any]:
        """Pick the winning variant — its template is then sent to every
        remaining lead going through the campaign."""
        if variant not in ("A", "B"):
            raise ValueError(f"variant must be 'A' or 'B', got {variant!r}")
        return self._request(
            "POST", f"sequences/{sequence_id}/steps/{step_id}/ab-test/winner",
            json={"variant": variant})

    # --- Schedules ---
    #
    # A schedule is a sending WINDOW (days, hours, timezone, pacing) owned by the
    # team, not by a campaign: several campaigns can share one, and a campaign can
    # carry several. Creating a campaign auto-creates one and returns its id in
    # `scheduleIds`.

    def list_schedules(
        self,
        *,
        limit: int = None,
        offset: int = None,
        page: int = None,
        sort_order: str = None,
    ) -> Dict[str, Any]:
        """List the team's schedules (paginated). Returns `{schedules: [...], ...}`."""
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if page is not None:
            params["page"] = page
        if sort_order is not None:
            params["sortBy"] = "createdAt"
            params["sortOrder"] = sort_order
        return self._request("GET", "schedules", params=params)

    def get_schedule(self, schedule_id: str) -> Dict[str, Any]:
        """Get one schedule."""
        return self._request("GET", f"schedules/{schedule_id}")

    def create_schedule(
        self,
        name: str,
        *,
        timezone: str = "Europe/Paris",
        start: str = "09:00",
        end: str = "18:00",
        weekdays: List[int] = None,
        seconds_to_wait: int = None,
        public: bool = None,
    ) -> Dict[str, Any]:
        """Create a sending window.

        Args:
            name: Schedule name.
            timezone: IANA zone the hours are read in.
            start / end: `HH:mm`, 24h.
            weekdays: Active days, 1 = Monday … 7 = Sunday. Defaults to Mon-Fri.
            seconds_to_wait: Pacing between two sends (server default 1200).
            public: Expose it as a template to the team.

        `name`, `timezone`, `start`, `end` and `weekdays` are all REQUIRED by the
        API — the defaults here fill them in rather than let a partial body 400.
        """
        weekdays = [1, 2, 3, 4, 5] if weekdays is None else weekdays
        self._check_schedule(
            start=start, end=end, weekdays=weekdays, timezone=timezone)
        body: Dict[str, Any] = {
            "name": name, "timezone": timezone,
            "start": start, "end": end, "weekdays": weekdays,
        }
        if seconds_to_wait is not None:
            body["secondsToWait"] = seconds_to_wait
        if public is not None:
            body["public"] = public
        return self._request("POST", "schedules", json=body)

    def update_schedule(self, schedule_id: str, data: dict) -> Dict[str, Any]:
        """Update a schedule (PATCH — only the keys sent change).

        Same vocabulary as `create_schedule`: `name`, `timezone`, `start`,
        `end`, `weekdays`, `secondsToWait`, `public`.
        """
        self._check_schedule(
            start=data.get("start"), end=data.get("end"),
            weekdays=data.get("weekdays"), timezone=data.get("timezone"))
        return self._request("PATCH", f"schedules/{schedule_id}", json=data)

    def delete_schedule(self, schedule_id: str) -> Dict[str, Any]:
        """Delete a schedule."""
        return self._request("DELETE", f"schedules/{schedule_id}")

    def get_campaign_schedules(self, campaign_id: str) -> List[Dict[str, Any]]:
        """List the schedules attached to a campaign.

        ⚠️ The documented path carries a TRAILING SLASH
        (`/campaigns/{id}/schedules/`) — kept verbatim.
        """
        return self._request("GET", f"campaigns/{campaign_id}/schedules/")

    def associate_schedule(self, campaign_id: str, schedule_id: str) -> Dict[str, Any]:
        """Attach an existing schedule to a campaign."""
        return self._request(
            "POST", f"campaigns/{campaign_id}/schedules/{schedule_id}")

    _TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

    @classmethod
    def _check_schedule(cls, *, start=None, end=None, weekdays=None, timezone=None) -> None:
        """Reject a malformed window locally — `start`/`end` are `HH:mm` and
        `weekdays` are 1..7, both patterns lemlist only answers with a bare 400."""
        for label, value in (("start", start), ("end", end)):
            if value is not None and not cls._TIME_RE.match(value):
                raise ValueError(f"{label} must be HH:mm (24h), got {value!r}")
        if weekdays is not None:
            bad = [d for d in weekdays if not isinstance(d, int) or not 1 <= d <= 7]
            if bad:
                raise ValueError(
                    f"weekdays must be ints 1 (Monday) to 7 (Sunday), got {bad!r}")
        if timezone is not None and "/" not in timezone and timezone != "UTC":
            raise ValueError(
                f"timezone must be an IANA name (e.g. 'Europe/Paris'), got {timezone!r}")

    # --- Campaign Tree ---

    def get_campaign_tree(self, campaign_id: str) -> Dict[str, Any]:
        """Get full campaign structure with sequences organized by level.

        Returns a tree structure with sequences dict, steps_flat list (depth-first),
        and branch information for conditionals.
        """
        campaign = self.get_campaign(campaign_id)
        sequences_raw = self.get_sequences(campaign_id)

        sequences = {}
        root_sequence = None

        for seq_id, seq_data in sequences_raw.items():
            level = seq_data.get('level', 0)
            if level == 0:
                root_sequence = seq_id

            steps = []
            for step in seq_data.get('steps', []):
                step_clean = {
                    'id': step.get('_id'),
                    'type': step.get('type'),
                    'delay': step.get('delay'),
                }
                if step.get('type') == 'email':
                    step_clean['subject'] = step.get('subject', '')
                    step_clean['message'] = step.get('message', '')
                elif step.get('type') in ('linkedinInvite', 'linkedinMessage', 'linkedinSend'):
                    step_clean['message'] = step.get('message', '')
                elif step.get('type') == 'conditional':
                    conditions = step.get('conditions', [])
                    step_clean['branches'] = []
                    for cond in conditions:
                        branch = {
                            'sequence_id': cond.get('sequenceId'),
                            'label': cond.get('label', 'Fallback' if cond.get('fallback') else 'Unknown'),
                            'fallback': cond.get('fallback', False),
                        }
                        if cond.get('key'):
                            branch['key'] = cond['key']
                        step_clean['branches'].append(branch)

                steps.append(step_clean)

            sequences[seq_id] = {
                'id': seq_id,
                'level': level,
                'steps': steps,
            }

        # Build flat list in execution order (depth-first traversal)
        steps_flat = []

        def traverse(seq_id: str, path: str = 'root'):
            if seq_id not in sequences:
                return
            seq = sequences[seq_id]
            for step in seq['steps']:
                steps_flat.append({
                    'sequence': seq_id,
                    'step': step,
                    'path': path,
                    'level': seq['level'],
                })
                if step.get('type') == 'conditional' and step.get('branches'):
                    for branch in step['branches']:
                        child_seq = branch.get('sequence_id')
                        if child_seq:
                            branch_label = branch.get('label', 'branch')
                            child_path = f"{path} > {branch_label}"
                            traverse(child_seq, child_path)

        if root_sequence:
            traverse(root_sequence)

        return {
            'id': campaign_id,
            'name': campaign.get('name', ''),
            'status': campaign.get('status', 'unknown'),
            'root_sequence': root_sequence,
            'sequences': sequences,
            'steps_flat': steps_flat,
        }

    def save_campaign_tree(self, campaign_id: str, directory: Path = None, tree: dict = None) -> Path:
        """Save campaign tree to local JSON file.

        Args:
            campaign_id: Campaign ID
            directory: Directory to save to (default: current directory)
            tree: Optional pre-fetched tree (will fetch if not provided)

        Returns:
            Path to saved file
        """
        if tree is None:
            tree = self.get_campaign_tree(campaign_id)

        save_dir = Path(directory) if directory else Path.cwd()
        save_dir.mkdir(parents=True, exist_ok=True)
        tree['synced_at'] = datetime.now().isoformat()

        filepath = save_dir / f"{campaign_id}.json"
        filepath.write_text(json.dumps(tree, indent=2, ensure_ascii=False))
        return filepath

    @staticmethod
    def load_campaign_tree(campaign_id: str, directory: Path = None) -> Optional[dict]:
        """Load campaign tree from local cache.

        Args:
            campaign_id: Campaign ID
            directory: Directory to load from (default: current directory)

        Returns:
            Campaign tree dict or None if not cached
        """
        load_dir = Path(directory) if directory else Path.cwd()
        filepath = load_dir / f"{campaign_id}.json"
        if filepath.exists():
            return json.loads(filepath.read_text())
        return None

    def sync_campaign(self, campaign_id: str, directory: Path = None) -> dict:
        """Fetch campaign tree from API and save locally.

        Returns:
            Campaign tree dict
        """
        tree = self.get_campaign_tree(campaign_id)
        self.save_campaign_tree(campaign_id, directory=directory, tree=tree)
        return tree

    # --- Tree helpers (work on tree dicts from get_campaign_tree) ---

    @staticmethod
    def find_step(tree: dict, step_id: str) -> Optional[dict]:
        """Find a step by ID in a campaign tree."""
        for item in tree.get('steps_flat', []):
            if item['step'].get('id') == step_id:
                return item
        return None

    @staticmethod
    def get_first_email(tree: dict) -> Optional[dict]:
        """Get the first email step in execution order."""
        for item in tree.get('steps_flat', []):
            if item['step'].get('type') == 'email':
                return item
        return None

    @staticmethod
    def get_emails(tree: dict) -> List[dict]:
        """Get all email steps from a campaign tree."""
        return [item for item in tree.get('steps_flat', []) if item['step'].get('type') == 'email']

    @staticmethod
    def print_tree(tree: dict):
        """Print campaign tree in a readable format."""
        print(f"Campaign: {tree['name']}")
        print(f"Status: {tree['status']}")
        if tree.get('synced_at'):
            print(f"Synced: {tree['synced_at'][:16].replace('T', ' ')}")
        print()
        print("Sequence:")
        for item in tree['steps_flat']:
            step = item['step']
            indent = '  ' * item['level']
            delay = f"J+{step['delay']}" if step['delay'] else 'J+0'

            if step['type'] == 'email':
                label = step.get('subject', '')[:45]
                print(f"{indent}[{delay}] 📧 {label}")
            elif step['type'] == 'conditional':
                branches = [b['label'][:20] for b in step.get('branches', [])]
                print(f"{indent}[{delay}] ❓ {' | '.join(branches)}")
            elif step['type'] == 'linkedinVisit':
                print(f"{indent}[{delay}] 👁️  LinkedIn visit")
            elif step['type'] == 'linkedinInvite':
                print(f"{indent}[{delay}] 🤝 LinkedIn invite")
            elif step['type'] in ('linkedinSend', 'linkedinMessage'):
                print(f"{indent}[{delay}] 💬 LinkedIn message")
            elif step['type'] == 'phone':
                print(f"{indent}[{delay}] 📞 Phone call")
            else:
                print(f"{indent}[{delay}] {step['type']}")

    # --- Leads ---

    def add_lead(self, campaign_id: str, lead) -> Dict[str, Any]:
        """Add lead to campaign.

        Args:
            campaign_id: Campaign ID
            lead: Lead dataclass or dict with email + optional fields
        """
        if isinstance(lead, Lead):
            data = {}
            if lead.firstName:
                data["firstName"] = lead.firstName
            if lead.lastName:
                data["lastName"] = lead.lastName
            if lead.companyName:
                data["companyName"] = lead.companyName
            if lead.phone:
                data["phone"] = lead.phone
            if lead.linkedinUrl:
                data["linkedinUrl"] = lead.linkedinUrl
            email = lead.email
        elif isinstance(lead, dict):
            lead = dict(lead)  # copy to avoid mutating
            email = lead.pop("email")
            data = lead
        else:
            raise TypeError(f"lead must be Lead or dict, got {type(lead)}")

        return self._request("POST", f"campaigns/{campaign_id}/leads/{email}", json=data)

    def get_all_leads(self, campaign_id: str) -> List[Dict[str, Any]]:
        """Get all leads from a campaign via CSV export."""
        import csv
        import io

        csv_text = self.export_leads(campaign_id)
        if not csv_text.strip():
            return []
        reader = csv.DictReader(io.StringIO(csv_text))
        return list(reader)

    def delete_lead(self, campaign_id: str, email: str) -> Dict[str, Any]:
        """Remove lead from campaign."""
        return self._request("DELETE", f"campaigns/{campaign_id}/leads/{email}")

    def create_lead(
        self,
        campaign_id: str,
        lead: Dict[str, Any],
        *,
        deduplicate: bool = False,
        linkedin_enrichment: bool = False,
        find_email: bool = False,
        verify_email: bool = False,
        find_phone: bool = False,
    ) -> Dict[str, Any]:
        """Create a lead in a campaign (current API: POST /campaigns/{id}/leads/,
        email in the body — distinct from the legacy `add_lead`/{email}-in-path
        endpoint above).

        Args:
            campaign_id: Campaign ID.
            lead: Lead fields — email, firstName, lastName, companyName, jobTitle,
                linkedinUrl, picture, phone, companyDomain, icebreaker, timezone
                (IANA, e.g. "Europe/Paris"), contactOwner (user ID or login
                email). All optional per the API; any other key is stored as a
                custom variable.
            deduplicate: Skip the insert if the email already exists in another
                campaign.
            linkedin_enrichment: Run LinkedIn enrichment on the lead.
            find_email: Find a verified email for the lead.
            verify_email: Verify the lead's existing email (debounce).
            find_phone: Find a phone number for the lead.

        Returns the created lead, including `_id` (used by `launch_lead` and
        `add_lead_variables`).
        """
        params = self._flag_params(
            deduplicate=deduplicate,
            linkedinEnrichment=linkedin_enrichment,
            findEmail=find_email,
            verifyEmail=verify_email,
            findPhone=find_phone,
        )
        return self._request(
            "POST", f"campaigns/{campaign_id}/leads/", json=lead, params=params,
        )

    def launch_lead(self, lead_id: str) -> Dict[str, Any]:
        """Launch a lead pending manual review (POST /leads/review/{leadId}).

        Only relevant for a campaign with review-before-send enabled — such a
        campaign leaves a newly created lead paused for review until launched.
        Returns `{"ok": true}` on success; a 400 raises `UpstreamHTTPError` whose
        body carries a lemlist error code, e.g.
        `CAMPAIGN_LEAD_REVIEW_LEAD_ALREADY_LAUNCHED`,
        `CAMPAIGN_LEAD_REVIEW_LEAD_PAUSED`,
        `CAMPAIGN_LEAD_REVIEW_LEAD_AI_VARIABLE_INVALID`,
        `CAMPAIGN_LEAD_REVIEW_LEAD_NO_SENDER_AVAILABLE`,
        `CAMPAIGN_LEAD_REVIEW_CAMPAIGN_STEP_ERRORS`.
        """
        return self._request("POST", f"leads/review/{lead_id}")

    def add_lead_variables(self, lead_id: str, variables: Dict[str, str]) -> Dict[str, Any]:
        """Add/set custom variables on a lead (POST /leads/{leadId}/variables).

        `variables` is sent as query parameters — the lemlist API contract for
        this endpoint (arbitrary keys, e.g. `{"customField1": "..."}`), not a
        JSON body.
        """
        return self._request("POST", f"leads/{lead_id}/variables", params=variables)

    def export_leads(self, campaign_id: str, state: str = None) -> str:
        """Export leads from campaign as CSV."""
        self._rate_limit()
        params = {}
        if state:
            params["state"] = state

        response = requests.get(
            f"{self.BASE_URL}/campaigns/{campaign_id}/export",
            headers={"Authorization": self._get_auth_header()},
            params=params
        , timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.text

    # --- Enrichment ---
    #
    # Standalone enrichment, distinct from the flags on `create_lead` (which only
    # enrich a lead on its way into a campaign). Every call is ASYNC: the POST
    # returns an id in ~1s, the work runs server-side, and `get_enrichment` polls
    # it. Each action spends lemlist enrichment credits.

    #: Enrichment actions, as the v1 query flags (`/enrich`, `/leads/{id}/enrich`).
    ENRICH_FLAGS = {
        "find_email": "findEmail",
        "verify_email": "verifyEmail",
        "linkedin_enrichment": "linkedinEnrichment",
        "find_phone": "findPhone",
    }
    #: The same actions as the v2 bulk vocabulary — deliberately NOT a snake_case
    #: of the v1 names: lemlist calls email verification `verify`, not `verify_email`.
    ENRICH_BULK_ACTIONS = {
        "find_email": "find_email",
        "verify_email": "verify",
        "linkedin_enrichment": "linkedin_enrichment",
        "find_phone": "find_phone",
    }

    def enrich(
        self,
        *,
        email: str = None,
        linkedin_url: str = None,
        first_name: str = None,
        last_name: str = None,
        company_name: str = None,
        company_domain: str = None,
        find_email: bool = False,
        verify_email: bool = False,
        linkedin_enrichment: bool = False,
        find_phone: bool = False,
        webhook_url: str = None,
    ) -> Dict[str, Any]:
        """Submit a standalone enrichment (POST /enrich) — no campaign, no lead.

        Args:
            email, linkedin_url, first_name, last_name, company_name,
                company_domain: the identity to enrich. All optional on paper;
                lemlist matches on whatever it gets, so a LinkedIn URL or a
                name + company domain is what actually resolves.
            find_email: find a verified email.
            verify_email: verify the email passed in (debounce).
            linkedin_enrichment: run the LinkedIn enrichment.
            find_phone: find a phone number.
            webhook_url: notified when the enrichment completes, instead of polling.

        At least one action is required — lemlist answers 400 otherwise.

        Returns `{"id": "enr_..."}`; pass that id to `get_enrichment`.
        """
        params = self._flag_params(
            findEmail=find_email,
            verifyEmail=verify_email,
            linkedinEnrichment=linkedin_enrichment,
            findPhone=find_phone,
        )
        if not params:
            raise ValueError(
                "no enrichment requested — set at least one of find_email, "
                "verify_email, linkedin_enrichment, find_phone"
            )
        params.update({
            k: v for k, v in {
                "email": email,
                "linkedinUrl": linkedin_url,
                "firstName": first_name,
                "lastName": last_name,
                "companyName": company_name,
                "companyDomain": company_domain,
                "webhookUrl": webhook_url,
            }.items() if v is not None
        })
        return self._request("POST", "enrich", params=params)

    def get_enrichment(self, enrich_id: str) -> Dict[str, Any]:
        """Poll an enrichment (GET /enrich/{enrichId}).

        Returns `{enrichmentId, enrichmentStatus, input, data}`. `enrichmentStatus`
        is the field to branch on — `in-progress` (HTTP 202), `done` (200) or
        `not-found` (404, a legitimate payload rather than an error, so it is
        returned instead of raised). `data` holds the found fields once done —
        shapes observed live, beyond the published schema: `email` carries
        `email` plus a verification `status` (`deliverable`/`undeliverable`),
        `phone` carries `phone`, `linkedin` carries a full profile, or `{}`
        when it could not be resolved.

        Two live caveats. `notFound` is not reliable — seen `false` on a
        payload with no number. And `done` does not guarantee the payload has
        landed: lemlist sometimes flips the status first and fills `data` on a
        later poll, so re-read once before concluding nothing was found.
        """
        return self._request("GET", f"enrich/{enrich_id}", tolerate=(404,))

    def enrich_lead(
        self,
        lead_id: str,
        *,
        find_email: bool = False,
        verify_email: bool = False,
        linkedin_enrichment: bool = False,
        find_phone: bool = False,
        webhook_url: str = None,
    ) -> Dict[str, Any]:
        """Enrich a lead already in a campaign (POST /leads/{leadId}/enrich).

        Same actions as `enrich`, but the identity comes from the existing lead
        and the result is written back onto it. Returns `{"id": "enr_..."}`,
        pollable with `get_enrichment`.

        Only accepted for a lead still AWAITING REVIEW: a reviewed lead — every
        lead in a campaign without review-before-send — gets
        `400 {"error": "lemrich is not available for lead reviewed"}`.
        """
        params = self._flag_params(
            findEmail=find_email,
            verifyEmail=verify_email,
            linkedinEnrichment=linkedin_enrichment,
            findPhone=find_phone,
        )
        if not params:
            raise ValueError(
                "no enrichment requested — set at least one of find_email, "
                "verify_email, linkedin_enrichment, find_phone"
            )
        if webhook_url:
            params["webhookUrl"] = webhook_url
        return self._request("POST", f"leads/{lead_id}/enrich", params=params)

    def bulk_enrich(
        self, items: List[Dict[str, Any]], webhook_url: str = None,
    ) -> List[Dict[str, Any]]:
        """Submit several enrichments in one call (POST /v2/enrichments/bulk).

        Args:
            items: one entry per person, each
                `{"input": {linkedinUrl|email|firstName|lastName|companyName|
                companyDomain}, "enrichmentRequests": [...], "metadata": {...}}`.
                `enrichmentRequests` uses the v2 vocabulary — `find_email`,
                `find_phone`, `verify`, `linkedin_enrichment` (see
                `ENRICH_BULK_ACTIONS`). `metadata` is echoed back, use it to
                match ids to your own rows.
            webhook_url: notified as each enrichment completes.

        Returns one entry per item, in order — `{"id": "enr_...", "metadata": ...}`
        or `{"error": "MISSING_INPUTS", "metadata": ...}`. Unlike a FullEnrich
        job, a bulk submit yields N ids, not one: poll each with `get_enrichment`.
        """
        params = {"webhookUrl": webhook_url} if webhook_url else None
        return self._request("POST", "v2/enrichments/bulk", json=items, params=params)

    # --- Activities & Stats ---

    def get_activities(self, campaign_id: str = None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get activities (lead interactions).

        Args:
            campaign_id: Optional campaign filter
            limit: Max results (default 100)
            offset: Pagination offset
        """
        params = {'limit': limit, 'offset': offset}
        if campaign_id:
            params['campaignId'] = campaign_id
        return self._request("GET", "activities", params=params)

    def sync_activities(self, campaign_id: str = None, since: str = None, max_pages: int = 50) -> List[Dict]:
        """Fetch all activities with pagination.

        Args:
            campaign_id: Optional campaign filter
            since: Optional ISO date string to filter activities after this date
            max_pages: Maximum number of pages to fetch (default 50 = 5000 activities)
        """
        all_activities = []
        offset = 0
        limit = 100
        pages = 0
        while pages < max_pages:
            batch = self.get_activities(campaign_id, limit, offset)
            if not batch:
                break
            if since:
                batch = [a for a in batch if a.get('createdAt', '') >= since]
            all_activities.extend(batch)
            offset += limit
            pages += 1
            if len(batch) < limit:
                break
            if since and batch and all(a.get('createdAt', '') < since for a in batch):
                break
        return all_activities

    #: Channels accepted by the v2 stats endpoints.
    STATS_CHANNELS = ("email", "linkedin", "others")

    def get_campaign_stats_v2(
        self,
        campaign_id: str,
        *,
        start_date: str,
        end_date: str,
        send_user: str = None,
        ab_selected: str = None,
        channels: List[str] = None,
    ) -> Dict[str, Any]:
        """Campaign stats from lemlist's OWN counters (`GET /v2/campaigns/{id}/stats`).

        Distinct from `get_campaign_stats` below, which derives numbers from a
        page of activities and therefore under-counts any campaign bigger than
        that page. Prefer this one.

        Args:
            start_date / end_date: ISO 8601, both REQUIRED by the API.
            send_user: `usr_…|sender@email` — BOTH halves are mandatory when set.
            ab_selected: `A` or `B`, to read one side of a running A/B test.
            channels: Subset of `STATS_CHANNELS`. Sent as a JSON array STRING —
                the endpoint reads it as a query parameter, not as repeated keys.

        Returns lead-level counters (`nbLeads`, `nbLeadsReached`,
        `nbLeadsAnswered`…), message-level counters (`messagesSent`, `opened`,
        `clicked`, `replied`, `messagesBounced`…), plus `perChannel` and a
        per-step `steps` array.
        """
        params: Dict[str, Any] = {"startDate": start_date, "endDate": end_date}
        if send_user is not None:
            if "|" not in send_user:
                raise ValueError(
                    "send_user must be 'usr_xxx|sender@email' — both halves are "
                    f"required by the API, got {send_user!r}")
            params["sendUser"] = send_user
        if ab_selected is not None:
            if ab_selected not in ("A", "B"):
                raise ValueError(
                    f"ab_selected must be 'A' or 'B', got {ab_selected!r}")
            params["ABSelected"] = ab_selected
        if channels is not None:
            self._check_channels(channels)
            params["channels"] = json.dumps(channels)
        return self._request(
            "GET", f"v2/campaigns/{campaign_id}/stats", params=params)

    def get_batch_campaign_stats(
        self,
        campaign_ids: List[str],
        *,
        start_date: str,
        end_date: str,
        send_user: str = None,
        ab_selected: str = None,
        channels: List[str] = None,
    ) -> Dict[str, Any]:
        """Same counters as `get_campaign_stats_v2`, for up to 100 campaigns in
        ONE call (`POST /v2/campaigns/stats/batch`).

        Here `channels` is a real JSON ARRAY in the body — not the stringified
        one the single-campaign query parameter wants. Same vocabulary, two
        encodings; sending the wrong one is silently ignored upstream.
        """
        if not campaign_ids:
            raise ValueError("campaign_ids is empty — nothing to fetch")
        if len(campaign_ids) > 100:
            raise ValueError(
                f"at most 100 campaigns per batch, got {len(campaign_ids)}")
        body: Dict[str, Any] = {
            "campaignIds": campaign_ids,
            "startDate": start_date,
            "endDate": end_date,
        }
        if send_user is not None:
            if "|" not in send_user:
                raise ValueError(
                    "send_user must be 'usr_xxx|sender@email' — both halves are "
                    f"required by the API, got {send_user!r}")
            body["sendUser"] = send_user
        if ab_selected is not None:
            if ab_selected not in ("A", "B"):
                raise ValueError(
                    f"ab_selected must be 'A' or 'B', got {ab_selected!r}")
            body["ABSelected"] = ab_selected
        if channels is not None:
            self._check_channels(channels)
            body["channels"] = channels
        return self._request("POST", "v2/campaigns/stats/batch", json=body)

    @classmethod
    def _check_channels(cls, channels: List[str]) -> None:
        bad = [c for c in channels if c not in cls.STATS_CHANNELS]
        if bad:
            raise ValueError(
                f"unknown channels {bad} — expected any of {cls.STATS_CHANNELS}")

    def get_campaign_reports(self, campaign_ids: List[str]) -> List[Dict[str, Any]]:
        """Cross-campaign report rows (`GET /campaigns/reports`).

        One row per campaign, in the operator's vocabulary (`emailsSent`,
        `emailsOpened`, `emailsReplied`, `senderNames`, `state`…) rather than
        the v2 stats one — the right shape for "compare mes campagnes".
        Campaign ids travel as ONE comma-joined query parameter.
        """
        if not campaign_ids:
            raise ValueError("campaign_ids is empty — nothing to report on")
        return self._request(
            "GET", "campaigns/reports",
            params={"campaignIds": ",".join(campaign_ids)})

    def get_campaign_stats(self, campaign_id: str) -> Dict[str, Any]:
        """Get stats for a campaign from activities.

        ⚠️ Derived from ONE page of activities (1000 max) — it under-counts any
        campaign bigger than that. `get_campaign_stats_v2` reads lemlist's own
        counters and has no such ceiling; prefer it.
        """
        activities = self.get_activities(campaign_id=campaign_id, limit=1000)
        counts = Counter(a.get('type') for a in activities)
        return {
            'total_activities': len(activities),
            'emails_sent': counts.get('emailsSent', 0),
            'emails_opened': counts.get('emailsOpened', 0),
            'emails_replied': counts.get('emailsReplied', 0),
            'emails_bounced': counts.get('emailsBounced', 0),
            'linkedin_visits': counts.get('linkedinVisitDone', 0),
            'linkedin_invites': counts.get('linkedinInviteDone', 0),
            'linkedin_messages': counts.get('linkedinSent', 0),
            'linkedin_accepted': counts.get('linkedinInviteAccepted', 0),
            'by_type': dict(counts),
        }

    # --- Status ---

    def status(self) -> Dict[str, Any]:
        """Check API connection status.

        Deliberately ONE page: this is a connection probe, and it is called on
        paths where a multi-page walk would be paid for nothing. When the page
        comes back full the count is a floor, and says so (`campaigns_capped`)
        rather than passing 100 off as the total.
        """
        try:
            campaigns = self.list_campaigns(limit=self.PAGE_MAX)
            return {
                "connected": True,
                "campaigns_count": len(campaigns),
                "campaigns_capped": len(campaigns) >= self.PAGE_MAX,
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}
