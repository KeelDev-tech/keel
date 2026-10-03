"""Non-authoritative, value-free review signals for directly comparable facts."""
import re
from urllib.parse import urlsplit


def _text(value):
    if isinstance(value, dict):
        value = value.get('value')
    return value.strip() if isinstance(value, str) and value.strip() else None


def _phone(value):
    match = re.fullmatch(r'(\+?[\d ().-]+?)(?:\s*(?:ext\.?|x)\s*(\d+))?', value, re.I)
    if not match:
        return None
    digits = re.sub(r'\D', '', match[1])
    return (digits, match[2]) if digits else None


def _email(value):
    if value.count('@') != 1 or any(c.isspace() for c in value):
        return None
    local, domain = value.split('@')
    return (local, domain.casefold()) if local and domain else None


def _linkedin(value):
    if any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit(value if '://' in value else 'https://' + value)
        if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
                or parsed.port or parsed.hostname not in ('linkedin.com', 'www.linkedin.com')):
            return None
        return (parsed.path.rstrip('/'), parsed.query, parsed.fragment)
    except ValueError:
        return None


def compare(profile, bank):
    """Compare contact values, not their truth or authority; never mutate inputs.

    Phone country codes are not guessed. URL paths/queries stay case sensitive.
    Résumé chronology is intentionally not an experience or career-start oracle.
    """
    result = {'status': 'UNAVAILABLE', 'findings': [], 'compared_fields': [],
              'unavailable_fields': [], 'experience_comparison': 'NOT_COMPARABLE'}
    if (not isinstance(profile, dict) or not isinstance(profile.get('contact'), dict)
            or not isinstance(bank, dict) or not isinstance(bank.get('answers'), dict)):
        return result
    for field, normalize in (('email', _email), ('phone', _phone), ('linkedin', _linkedin)):
        left = _text(profile['contact'].get(field))
        right = _text(bank['answers'].get(field))
        left = normalize(left) if left else None
        right = normalize(right) if right else None
        if left is None or right is None:
            result['unavailable_fields'].append(field)
            continue
        result['compared_fields'].append(field)
        if left != right:
            result['findings'].append({'field': field, 'code': 'profile_answer_difference', 'status': 'REVIEW'})
    if result['compared_fields']:
        result['status'] = 'REVIEW' if result['findings'] else 'NO_DIFFERENCES_IN_COMPARED_FIELDS'
    return result
