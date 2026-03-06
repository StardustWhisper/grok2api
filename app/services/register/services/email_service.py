"""Email service for temporary inbox creation."""
from __future__ import annotations

import os
import random
import string
from typing import Tuple, Optional

import requests

from app.core.config import get_config


class EmailService:
    """Email service wrapper.

    Supports two variants of the Cloudflare temp mail worker:

    - Legacy admin API: POST /admin/new_address with header `x-admin-auth`
    - User API: POST /api/new_address with header `x-custom-auth` (private-site password)

    Note: When the worker has `needAuth=true`, most endpoints (including /api/mails)
    require `x-custom-auth` *in addition* to mailbox JWT.
    """

    def __init__(
        self,
        worker_domain: Optional[str] = None,
        email_domain: Optional[str] = None,
        admin_password: Optional[str] = None,
        site_password: Optional[str] = None,
        use_api_new_address: Optional[bool] = None,
    ) -> None:
        self.worker_domain = (
            (worker_domain or get_config("register.worker_domain", "") or os.getenv("WORKER_DOMAIN", "")).strip()
        )
        self.email_domain = (
            (email_domain or get_config("register.email_domain", "") or os.getenv("EMAIL_DOMAIN", "")).strip()
        )

        # Legacy admin password for /admin/* endpoints.
        self.admin_password = (
            (admin_password or get_config("register.admin_password", "") or os.getenv("ADMIN_PASSWORD", "")).strip()
        )

        # Private-site password (x-custom-auth) used when needAuth=true.
        # Fall back to admin_password for backward compatibility.
        self.site_password = (
            (site_password or get_config("register.site_password", "") or os.getenv("SITE_PASSWORD", "")).strip()
            or self.admin_password
        )

        if use_api_new_address is None:
            cfg = get_config("register.use_api_new_address", False)
            if isinstance(cfg, bool):
                use_api_new_address = cfg
            else:
                use_api_new_address = str(cfg).strip().lower() in {"1", "true", "yes", "on"}
        self.use_api_new_address = bool(use_api_new_address)

        if not self.worker_domain or not self.email_domain:
            raise ValueError(
                "Missing required email settings: register.worker_domain, register.email_domain"
            )

        if self.use_api_new_address:
            # /api/new_address requires x-custom-auth when private-site password is enabled.
            if not self.site_password:
                raise ValueError(
                    "Missing required email settings for /api/new_address: register.site_password (x-custom-auth)"
                )
        else:
            # /admin/new_address requires x-admin-auth.
            if not self.admin_password:
                raise ValueError(
                    "Missing required email settings for /admin/new_address: register.admin_password (x-admin-auth)"
                )

    def _generate_random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def create_email(self) -> Tuple[Optional[str], Optional[str]]:
        """Create a temporary mailbox. Returns (jwt, address)."""
        if self.use_api_new_address:
            # cloudflare_temp_email user API
            url = f"https://{self.worker_domain}/api/new_address"
            payload = {
                "name": self._generate_random_name(),
                "domain": self.email_domain,
                # If the worker enables Turnstile check, caller may need to provide a cf_token.
                # We keep it empty here; configure the worker to allow it or disable check.
                "cf_token": "",
            }
            headers = {
                "x-custom-auth": self.site_password,
                "Content-Type": "application/json",
            }
        else:
            # legacy admin API
            url = f"https://{self.worker_domain}/admin/new_address"
            payload = {
                "enablePrefix": True,
                "name": self._generate_random_name(),
                "domain": self.email_domain,
            }
            headers = {
                "x-admin-auth": self.admin_password,
                # Some deployments also require private-site password.
                "x-custom-auth": self.site_password,
                "Content-Type": "application/json",
            }

        try:
            res = requests.post(url, json=payload, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            print(f"[-] Email create failed: {res.status_code} - {res.text}")
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"[-] Email create error ({url}): {exc}")
        return None, None

    def fetch_first_email(self, jwt: str) -> Optional[str]:
        """Fetch the first email content for the mailbox."""
        try:
            res = requests.get(
                f"https://{self.worker_domain}/api/mails",
                params={"limit": 10, "offset": 0},
                headers={
                    # cloudflare_temp_email requires both headers when needAuth=true
                    "x-custom-auth": self.site_password,
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"Email fetch failed: {exc}")
            return None
