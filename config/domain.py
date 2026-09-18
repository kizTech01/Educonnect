"""Domain-derived Django security settings for the shared tenant platform."""


def parse_csv(value):
    """Return non-empty comma-separated values without changing their meaning."""
    return [item.strip() for item in value.split(",") if item.strip()]


def unique(values):
    """Keep configuration deterministic while preserving its declared order."""
    return list(dict.fromkeys(values))


def core_domain_settings(core_domain):
    """Build the host and HTTPS CSRF allowlists for one platform domain."""
    domain = core_domain.strip().lower().strip(".")
    if not domain:
        return domain, [], []

    return (
        domain,
        [domain, f".{domain}"],
        [f"https://{domain}", f"https://*.{domain}"],
    )
