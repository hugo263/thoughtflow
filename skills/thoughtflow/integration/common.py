"""Shared input rules; tags round-trip unchanged across the three destinations."""
import re
import unicodedata

from integration.getnote import GetNoteError


def validate_tags(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 5:
        raise GetNoteError('invalid_input', 'Use up to 5 tags.')
    result = []
    for tag in value:
        if not isinstance(tag, str):
            raise GetNoteError('invalid_input', 'Invalid tag format.')
        tag = unicodedata.normalize('NFC', tag.strip().lstrip('#'))
        valid = (0 < len(tag) <= 10 and not tag.startswith('/') and not tag.endswith('/')
                 and '//' not in tag and any(char.isalpha() for char in tag)
                 and all(char.isalnum() or char in '_-/' for char in tag))
        if not valid:
            raise GetNoteError('invalid_input', 'Each tag needs a letter and at most 10 characters. Use letters, numbers, underscores, hyphens, or single internal slashes.')
        if tag not in result:
            result.append(tag)
    return result


def valid_target(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value))
