# DonutSMP Essentials Assistant

DonutSMP Essentials Assistant is a Discord operations assistant with private support tickets, secure middleman workflows, giveaways, staff applications, reputation profiles, audit logging, and a Flask transaction webhook. Tickets and webhook events are stored in `middleman.sqlite3`.

`config.py` owns environment parsing and startup validation. `database.py` owns SQLite sessions, numbered migrations, and verified rotating backups. `main.py` owns bot startup, shared services, and the Flask endpoint. Individual bot commands are implemented as cogs in `commands/`, with one module per command.

## Setup

Use Python 3.10 or newer. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications), invite it with permission to view/send messages, manage channels, and use application commands, and enable **Message Content Intent** and **Server Members Intent** under the bot's privileged gateway intents. Server Members Intent is required for join welcomes. The IDs supplied for the marketplace, welcome, panel, transcript, ticket category, and support role are configured by default. Confirm that they belong to the correct server and that the bot can send embeds in them and manage the ticket category. To copy IDs, enable Developer Mode in Discord and use the relevant right-click **Copy ID** menu.

Install dependencies and configure the environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set `DISCORD_TOKEN` in your shell, or create a `.env` file in the project directory. These channel IDs are configurable; the requested IDs are defaults:

```dotenv
DISCORD_TOKEN=your_bot_token
COMMAND_PREFIX=!
TRANSACTION_WEBHOOK_SECRET=use_a_long_random_secret
MIDDLEMEN_PANEL_CHANNEL_ID=1554210069988507829
MIDDLEMEN_CATEGORY_ID=1455842233528877151
MIDDLEMEN_SUPPORT_ROLE_ID=1455842100456329380
VOUCH_CHANNEL_ID=1554218114567241971
MARKET_SELLING_CHANNEL_ID=1554204131659354163
MARKET_BUYING_CHANNEL_ID=1554204024868179998
MARKETPLACE_CHANNEL_ID=1554206556877234207
MARKETPLACE_MANAGER_ROLE_ID=your_marketplace_manager_role_id
WELCOME_CHANNEL_ID=1455842291293093929
TICKET_TRANSCRIPT_CHANNEL_ID=1455842412483182708
SUPPORT_TICKET_PANEL_CHANNEL_ID=1455842333139538032
SUPPORT_TICKET_CATEGORY_ID=1455842228806357027
REPORT_TICKET_CATEGORY_ID=1554394002919260261
PARTNERSHIP_TICKET_CATEGORY_ID=1554945116878282902
VERIFICATION_CHANNEL_ID=1455842278936678431
VERIFIED_ROLE_IDS=1455842195855773730,1455842143980748864
PENDING_VERIFICATION_ROLE_ID=1455842194681364502
SUPPORT_ROLE_IDS=1455842100456329380,1455842098338336779,1455842096115351593
STAFF_DASHBOARD_CHANNEL_ID=1455842399589896329
AUDIT_LOG_CHANNEL_ID=1455842399589896329
TICKET_REMINDER_HOURS=24
TICKET_REVIEW_HOURS=72
WEBHOOK_OUTBOX_POLL_SECONDS=15
VOUCH_OVER_50_ROLE_ID=1555675245074190376
VOUCH_OVER_75_ROLE_ID=1455842135369842709
MODERATION_ROLE_ID=1455842096115351593
APPLICATION_CHANNEL_ID=1554948402683584526
ACCEPTANCE_CHANNEL_ID=1455842351263256737
# Optional: URL of the DonutSMP Essentials marketplace banner used by /middleman panel.
# MIDDLEMAN_BANNER_URL=https://cdn.discordapp.com/attachments/1455842372440162407/1555669376202444831/donutsmpessentialsbanner.png
# DISCORD_BACKUP_DIRECTORY=backups
# Optional: trusted DonutSMP API or plugin relay base URL. No passwords or session tokens are accepted.
# DONUTSMP_API_URL=https://trusted.example/api
# Optional: player existence endpoint; expected response is JSON such as {"exists": true}.
# DONUTSMP_PLAYER_API_URL=https://trusted.example/players
# DATABASE_BACKUP_PATH=middleman.sqlite3.backup
# DATABASE_BACKUP_INTERVAL_HOURS=24
# Optional: set this to sync slash commands immediately to one server.
DISCORD_GUILD_ID=your_server_id
# Optional: DATABASE_PATH=middleman.sqlite3
```

The assistant presence is **Do Not Disturb • Watching Keeping trades safe**.

Startup requires `DISCORD_TOKEN`, `TRANSACTION_WEBHOOK_SECRET`, every channel and role ID shown above, and positive integer values for those IDs. The bot exits before starting Flask or Discord when required configuration is missing or invalid.

Keep the token and webhook secret private. Do not commit `.env`; it is ignored by Git. The transaction webhook is disabled unless `TRANSACTION_WEBHOOK_SECRET` is set, and every request must include the matching `X-Webhook-Secret` header. Keep port 5000 behind a trusted network or HTTPS reverse proxy; Flask's built-in server is for development, not public production hosting.

Start both services together:

```bash
python main.py
```

The Flask server listens on port `5000`; the Discord bot runs alongside it. Without `DISCORD_GUILD_ID`, slash commands sync globally and may take time to appear. Set it during setup for immediate server-specific command registration.

## Middleman tickets

An administrator runs `/middleman panel` to post the marketplace panel in channel `1554210069988507829`. After a member presses **Start a deal**, they select one of the configured middlemen and choose **I am the buyer** or **I am the seller**. The bot creates a sequential `middleman-deal-N` channel and assigns a unique `ABC-123` deal code. The selected middleman is mentioned in the opening invitation and can accept or decline it. The ticket remains in the configured middleman category and keeps the existing ticket controls.

The buyer can report sending coins, but that is only a claim. Both participants must agree to any price change; the proposed amount stays pending until the other member accepts it. A support member must inspect DonutSMP in-game and use the **Verify payment** button in the deal before the seller can mark the item delivered. The buyer then confirms receipt. A support member must manually transfer the coins in-game and only afterward use **Record release** to record that release. Either participant can request **Close Deal**, but the channel locks and becomes `closed-deal-N` only after both agree. Support can use the staff-only **Staff close** button for review, while the staff dashboard shows the oldest open deals needing attention. Closing either way archives the full HTML transcript, including the public ticket timeline and attachment links, to channel `1455842412483182708`. Inactive tickets get participant reminders after `TICKET_REMINDER_HOURS` and a one-time support review flag after `TICKET_REVIEW_HOURS`. This bot cannot access balances, custody coins, or make any DonutSMP transfer. The **Problem** controls offer guidance and staff escalation. Ticket state and sequential numbering persist in SQLite across restarts.

`/help` provides an in-Discord guide to every command and workflow. Staff-only `/transactionhistory` queries only events received through this bot's webhook. `/vouch` and `/vouchcount` use Discord's server-member picker rather than free-text player names. `/vouch` privately collects a Legit/Scammer verdict, a 1-5 star rating, and a reason, then posts the styled report in `1554218114567241971`. Each Discord member can vouch once per selected member per server. `/vouchcount` shows total, Legit, Scammer, and average-star counts for the selected member; these are community reports, not verified DonutSMP records. Ticket creation, vouches, and giveaway entries use independent cooldowns; there is no global command cooldown.

## Giveaways

Administrators use `/giveaway create` to publish a professional timed giveaway. The command accepts a prize, duration (`10s` through `30d`, including combinations such as `1d 6h`), winner count, optional conditions, an optional required Discord role, and an optional destination channel. The post shows the host, prize, winner count, live entry count, eligibility requirement, and both relative and exact end times. Members enter with a button and can only enter once per giveaway. `/giveaway info [giveaway_id]` lets members inspect the giveaway and see whether they are entered.

Giveaways persist in SQLite and active entry buttons are restored after restarts. Expired giveaways are finalized automatically, with cryptographically strong random winner selection and a public winner announcement. Winner announcements instruct winners to open the support panel and choose **Giveaways**. Administrators can use `/giveaway active` to see the queue, `/giveaway end` to end one immediately, `/giveaway reroll` to select replacement winners from remaining entrants, or `/giveaway cancel` to end it without selecting a winner. Required Discord roles are checked at entry time; free-text conditions are displayed to members but are not automatically verified, so staff should use them for rules that can be checked manually.

## Support tickets, profiles, and operations

An administrator runs `/ticket create` to post the support panel in channel `1455842333139538032`. Members choose **General Problems Ticket**, **Report a Player Ticket**, **Giveaways**, or **Partnership Request**, then complete a reason modal before a private ticket is created. General and giveaway tickets use category `1455842228806357027`; player reports use category `1554394002919260261`; partnerships use `1554945116878282902`. Only the ticket creator, administrators, and roles `1455842100456329380`, `1455842098338336779`, and `1455842096115351593` can access these tickets.

Support staff can claim, close, reopen, and view the ticket queue with `/ticket claim`, `/ticket close`, `/ticket reopen`, and `/ticket active`. Deleting any ticket requires a confirmation embed, then a 10-second countdown with a Cancel button. Ticket actions, channel changes, permission changes, role changes, bans, timeouts, removals, warnings, and dashboard refreshes are stored in the append-only audit log and published to `1455842399589896329`.

Administrators can use `/apply` to post a professional staff application panel. Applicants confirm the start, then complete four guided sections in direct messages. Full questions are shown above the answer fields. Completed applications are sent to `1554948402683584526` with reason-required Accept, Reject, and Redo buttons, including the moderator who reviewed the application. Accepted and rejected decisions are announced in `1455842351263256737`; applicants receive a direct message with the next steps.

Administrators use `/verify` to post the verification panel in `1455842278936678431`. New members can only see that channel until they enter a valid Minecraft Java username and solve the CAPTCHA. The bot then applies roles `1455842195855773730` and `1455842143980748864`, removes pending-verification role `1455842194681364502` when present, and changes the member's server nickname. The bot requires Manage Channels, Manage Nicknames, Manage Roles, and a role position above the verified and pending-verification roles.

Administrators can use `/warn`, `/kick`, `/ban`, and `/timeout`. Timeout durations accept values such as `1m`, `2h`, `7d`, or `1d 6h`, up to Discord's 28-day maximum. `/appeal` creates a private general support ticket named `appeal-member-number`. Anti-spam, suspicious-link detection, raid alerts, ticket reminders, auto-closure, and database backups run through persistent background workers; staff can configure the protection switches and ticket timing with `/settings`.

`/profile <member>` shows joined date, completed middleman deals, vouch totals, Legit/Scammer ratios, and active staff warnings. Administrators can add warnings with `/warn`, remove members with `/kick`, permanently remove members with `/ban`, or temporarily restrict members with `/timeout`; `/staff warnings` lets configured staff review warnings. `/health` reports database, Discord, webhook, worker, error-counter, and DonutSMP integration status. `/dashboard refresh` publishes the current support, middleman, giveaway, warning, webhook, and transaction-mismatch queues to `1455842399589896329`.

Authenticated transaction webhooks are matched against open middleman deals by Minecraft usernames and expected amount. Exact matches and amount mismatches are recorded and included in staff transaction alerts. Automatic balances, transfers, and verification remain disabled until a trusted DonutSMP API or plugin is configured through `DONUTSMP_API_URL`; the bot never stores game passwords or session tokens.

## Custom embeds

Administrators can use `/embed create` to open the embed builder. Choose a destination channel, enter a title, description, hex color, footer, and image URL, optionally provide a thumbnail URL, preview the result privately, add up to 25 fields with inline Yes/No control, and publish only when it is ready. Published embeds are audit logged and user mentions are disabled by default.

## Transaction webhook

Send a JSON POST to `/transaction`. Include a stable, unique `event_id` for each source transaction and retain it across retries. Directional events should include `from_player` and `to_player`, both Minecraft Java usernames. `amount` must be a finite number or numeric string. Events are stored locally and durably queued for Discord delivery; they are not fetched from DonutSMP.

```bash
curl -X POST http://localhost:5000/transaction \
	-H 'Content-Type: application/json' \
	-H 'X-Webhook-Secret: use_a_long_random_secret' \
	-d '{"event_id":"donutsmp-transaction-12345","from_player":"Seller123","to_player":"Zlorbie788","amount":1250.50}'
```

The old `player` field is still accepted as a one-sided event, but it cannot identify the sender. `/transactionhistory [player]` searches both sides of directional events and shows event IDs. A DonutSMP plugin, API, or trusted relay must send these webhook requests; the bot cannot discover in-game transactions by itself.

New events return HTTP `202` with `{"status":"queued"}`. A retry with the same event ID and same transaction returns HTTP `200` with `{"status":"duplicate"}` and does not create another history row or alert. Reusing an event ID with different details returns `409`. Missing/invalid event IDs are rejected. Discord delivery failures are retried from a persistent SQLite outbox with exponential backoff, including after bot restarts.

## DonutSMP integration needed

There is no official game-history or escrow connection in this project. To obtain real transaction history or automatically verify/release coins, you must provide an authorized DonutSMP API, server plugin, or trusted webhook source that reports transactions for `Zlorbie788`, plus permission to use it. Do not provide account passwords or session tokens. Until that integration exists, staff must verify and transfer coins manually in-game; the bot records workflow confirmations only.