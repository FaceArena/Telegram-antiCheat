"""Pure helpers for classifying channel audience membership events."""
from aiogram.enums import ChatMemberStatus

_JOINED_FROM = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
_JOINED_TO = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
_ACTIVE_FROM = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR, ChatMemberStatus.RESTRICTED}
_LEFT_TO = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


def classify_membership_change(old_status: ChatMemberStatus, new_status: ChatMemberStatus) -> str | None:
    """Return 'join', 'leave', or None for non-subscription status changes."""
    if old_status in _JOINED_FROM and new_status in _JOINED_TO:
        return "join"
    if old_status in _ACTIVE_FROM and new_status in _LEFT_TO:
        return "leave"
    return None
