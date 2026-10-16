# Personal-agent updates

Coverage Brain leads with a concise briefing and an update stream. Client files and detailed agent activity are secondary views. The first screen answers what changed, why it needs attention, and what to do next.

## Behavior

- A completed real review produces entries for review needs, actual unsent drafts, and actual record proposals. Counts come from the stored result.
- Repeated reviews of the same case evidence do not create duplicate active-result notifications.
- Read/unread and one-hour snooze are separate from approving a review. Neither sends a message nor applies a customer-record change.
- The server checks its configured demo case file every 30 seconds while monitoring is enabled and the server is running. Valid source changes appear as new evidence to review. Invalid source changes preserve the last valid case and show a monitoring error.
- An in-flight review keeps its starting evidence snapshot. A review of an older source revision cannot be approved after new evidence arrives.
- Monitoring does not automatically spend model usage. A new four-agent review starts from the explicit review action.
- Closing the browser does not itself stop the server monitor, but a stopped or archived Daytona sandbox cannot keep monitoring. This is not an always-on email integration.

## Design references

[Muse's design article](https://introducing.muse.ai/) describes meaningful proactive messages, compact work status, deeper activity on demand, and explicit approval controls. We adapt those relationships to broker work.

[Grok Bot notifications](https://docs.x.ai/grok-bot/settings-and-notifications) distinguish work in progress, unread results and requests for attention. Our update states follow a similar hierarchy.

[Dot](https://new.computer/dot) is a historical reference for continuity and relevant personal context, not an integrated service or a claim about current availability.

The interface deliberately does not add an inert chat box. Existing agent actions remain functional, and evidence stays available through progressive disclosure.
