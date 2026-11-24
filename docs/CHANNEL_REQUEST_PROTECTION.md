# Channel join-request anti-raid protection

## Scope

This feature protects **Telegram channels that use join requests**. It does not inspect ordinary direct subscriptions, existing subscribers, view counts, or historical audience lists.

The feature is opt-in and is disabled for every channel by default.

## Required Telegram permission

Add KimGuard as a channel administrator with the `invite_users` right. Telegram requires this permission for the bot to decline join requests. The bot does not request post-editing or post-deletion rights for this feature.

## Current policy

When enabled, KimGuard counts unique user IDs that submit join requests in a rolling window:

- 8 unique requests in 10 seconds trigger a temporary lockdown.
- The triggering request and later requests during the lockdown are declined.
- The lockdown lasts 5 minutes.
- The threshold, window, and lockdown duration are stored in `chat_settings` as `request_limit`, `request_window`, and `request_lockdown_seconds`. The current private channel panel displays the default policy; editing those values is not yet exposed in the UI.

Repeated updates for the same user do not inflate the count because the event table has a `(chat_id, user_id)` primary key.

## Failure behavior

If PostgreSQL cannot determine the request rate, KimGuard fails open and leaves the request untouched. If Telegram rejects a decline operation, the bot records a failure event when possible and writes the error to its runtime log.

## Audit events

The channel journal records received requests, raid detection, successful automatic declines, failed decline attempts, and protection toggle changes.

## Limitations

- Telegram sends join-request updates only while the bot is an administrator and the `chat_join_request` update type is enabled.
- This does not detect fake accounts with certainty; it detects abnormal request bursts.
- It cannot stop users who join directly without submitting a join request.
- It cannot prevent Telegram-wide reports, scam labels, or platform-level restrictions.
