import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import main as core


APPLICATION_CHANNEL_ID = core.APPLICATION_CHANNEL_ID
ACCEPTANCE_CHANNEL_ID = core.ACCEPTANCE_CHANNEL_ID
APPLICATION_PANEL_CHANNEL_ID = core.APPLICATION_PANEL_CHANNEL_ID

QUESTIONS = [
    ("Discord User", "Your username#0000 or global username."),
    ("Minecraft Username", "Your exact premium IGN. Do not include unneeded dots or commas."),
    ("Age", "Your current age."),
    ("Why do you want to become a staff member?", "What motivates you to help the community?"),
    ("Activity Level", "How many hours per day or week can you realistically dedicate to staff duties?"),
    ("Staff Experience", "What moderation experience do you have? List the servers and why you are no longer staff there."),
    ("History with Minecraft & DonutSMP", "How long have you played Minecraft, and how long have you played DonutSMP specifically?"),
    ("Discord Moderation Knowledge", "On a scale of 1 to 10, how familiar are you with Audit Logs, Server Insights, AutoMod, timeouts, role permissions, and ticketing bots?"),
    ("Team Conflict Management", "What would you do if you strongly disagreed with another staff member's moderation decision?"),
    ("Team Value & Community Vision", "What qualities, skills, or perspectives do you bring? What makes a healthy and welcoming community?"),
    ("Growth Plan", "How do you plan on helping this server grow?"),
    ("Motivation", "How motivated are you to be staff on a scale from 1 to 10?"),
]

STEPS = [
    [0, 1, 2, 3, 4],
    [5, 6],
    [7, 8, 9],
    [10, 11],
]


def get_application(application_id):
    with core.db_session() as connection:
        row = connection.execute("SELECT * FROM staff_applications WHERE id = ?", (application_id,)).fetchone()
    return dict(row) if row else None


def update_application(application_id, **values):
    assignments = ", ".join(f"{key} = ?" for key in values)
    with core.db_session() as connection:
        connection.execute(f"UPDATE staff_applications SET {assignments} WHERE id = ?", (*values.values(), application_id))


def application_embed(application, answers=None):
    answers = answers or json.loads(application["answers"])
    embed = core.make_embed(
        f"Staff Application #{application['id']}",
        f"Applicant: <@{application['user_id']}>\nStatus: **{application['status'].title()}**",
        "info" if application["status"] == "pending" else "success" if application["status"] == "accepted" else "warning",
    )
    for index, (question, _) in enumerate(QUESTIONS):
        answer = answers.get(str(index), "No answer provided")
        embed.add_field(name=question, value=answer[:1024], inline=False)
    embed.add_field(
        name="Moderator who reviewed this application",
        value=f"<@{application['decided_by']}>" if application.get("decided_by") else "Not reviewed yet",
        inline=False,
    )
    if application.get("decision_reason"):
        embed.add_field(name="Staff decision reason", value=application["decision_reason"][:1024], inline=False)
    embed.set_footer(text=f"Submitted {application['created_at'][:16].replace('T', ' ')} UTC")
    return embed


class StartApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Staff Application", style=discord.ButtonStyle.primary, custom_id="staff-application:start")
    async def start(self, interaction, button):
        await interaction.response.send_message(
            embed=core.make_embed(
                "Confirm staff application",
                "The application takes four guided steps and will be completed in your Discord direct messages. Your answers will be sent to the staff review channel. Do you want to begin?",
                "info",
            ),
            view=ConfirmApplicationView(interaction.user.id),
            ephemeral=True,
        )


class ConfirmApplicationView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await core.send_ephemeral_embed(interaction, "Private application", "Only the member who started this application can continue.", "error")
            return False
        return True

    @discord.ui.button(label="Confirm and DM me", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        with core.db_session() as connection:
            cursor = connection.execute(
                "INSERT INTO staff_applications (guild_id, user_id, answers, created_at) VALUES (?, ?, ?, ?)",
                (interaction.guild_id, interaction.user.id, json.dumps({}), datetime.now(timezone.utc).isoformat()),
            )
            application_id = cursor.lastrowid
        try:
            await interaction.user.send(
                embed=application_step_embed(0),
                view=ApplicationStepView(application_id, 0),
            )
        except discord.Forbidden:
            update_application(application_id, status="cancelled", decision_reason="Applicant's direct messages are closed.")
            await core.send_ephemeral_embed(interaction, "Direct messages are closed", "Enable direct messages from this server, then start the application again.", "error")
            return
        await core.send_ephemeral_embed(interaction, "Application started", "I sent Part 1 to your direct messages. Please complete it there.", "success")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(embed=core.make_embed("Application cancelled", "No application was started."), view=None)


class ApplicationStepView(discord.ui.View):
    def __init__(self, application_id, step):
        super().__init__(timeout=1800)
        self.application_id = application_id
        self.step = step

    @discord.ui.button(label="Open this section", style=discord.ButtonStyle.primary)
    async def open_step(self, interaction, button):
        application = get_application(self.application_id)
        if not application or application["user_id"] != interaction.user.id or application["status"] != "pending":
            await core.send_ephemeral_embed(interaction, "Application unavailable", "This application is no longer accepting answers.", "warning")
            return
        modal = [StepOneModal, StepTwoModal, StepThreeModal, StepFourModal][self.step](self.application_id)
        await interaction.response.send_modal(modal)


def application_step_embed(step):
    embed = core.make_embed(
        f"Staff Application • Part {step + 1} of 4",
        "Select **Open this section** to answer the questions. The full question is shown above each answer field.",
        "info",
    )
    for index in STEPS[step]:
        question, prompt = QUESTIONS[index]
        embed.add_field(name=f"{index + 1}. {question}", value=prompt, inline=False)
    return embed


class ApplicationModal(discord.ui.Modal):
    step = 0

    def __init__(self, application_id):
        super().__init__(title=f"Staff Application • Part {self.step + 1} of 4")
        self.application_id = application_id
        for index in STEPS[self.step]:
            question, prompt = QUESTIONS[index]
            self.add_item(discord.ui.TextInput(label=question[:45], placeholder="Type your answer here", style=discord.TextStyle.paragraph, required=True, max_length=1000))

    async def on_submit(self, interaction):
        application = get_application(self.application_id)
        if not application or application["user_id"] != interaction.user.id or application["status"] != "pending":
            await core.send_ephemeral_embed(interaction, "Application unavailable", "This application is no longer accepting answers.", "warning")
            return
        answers = json.loads(application["answers"])
        for index, field in zip(STEPS[self.step], self.children):
            answers[str(index)] = str(field.value).strip()
        update_application(self.application_id, answers=json.dumps(answers))
        next_step = self.step + 1
        if next_step < len(STEPS):
            await interaction.response.send_message(
                embed=application_step_embed(next_step),
                view=ApplicationStepView(self.application_id, next_step),
            )
            return
        application = get_application(self.application_id)
        channel = await core.resolve_channel(APPLICATION_CHANNEL_ID)
        message = await channel.send(
            content=f"<@{application['user_id']}>",
            embed=application_embed(application, answers),
            view=ApplicationReviewView(self.application_id),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=application["user_id"])]),
        )
        update_application(self.application_id, review_message_id=message.id)
        await interaction.response.send_message(embed=core.make_embed("Application submitted", "Your application has been sent to the staff team for review.", "success"))


class StepOneModal(ApplicationModal):
    step = 0


class StepTwoModal(ApplicationModal):
    step = 1


class StepThreeModal(ApplicationModal):
    step = 2


class StepFourModal(ApplicationModal):
    step = 3


class DecisionReasonModal(discord.ui.Modal, title="Staff decision reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, min_length=5, max_length=1000, placeholder="Explain the decision clearly and professionally.")

    def __init__(self, application_id, decision):
        super().__init__()
        self.application_id = application_id
        self.decision = decision

    async def on_submit(self, interaction):
        application = get_application(self.application_id)
        if not application or application["status"] != "pending":
            await core.send_ephemeral_embed(interaction, "Application already decided", "This application no longer needs a decision.", "warning")
            return
        reason = str(self.reason.value).strip()
        update_application(self.application_id, status=self.decision, decision_reason=reason, decided_by=interaction.user.id)
        application = get_application(self.application_id)
        if self.decision == "redo":
            await notify_applicant(application, "Application redo requested", f"Staff asked you to redo your application.\n\nReason: {reason}\n\nPlease submit another application after addressing this feedback.")
        else:
            result = "accepted" if self.decision == "accepted" else "rejected"
            instruction = "Please open a General Problems Ticket and wait for staff to continue with you." if result == "accepted" else "You may submit another application after at least 48 hours."
            moderator = f"<@{interaction.user.id}>"
            result_embed = core.make_embed(f"Staff application {result}", f"Applicant: <@{application['user_id']}>\nModerator: {moderator}\n\n{instruction}\n\nStaff reason: {reason}", "success" if result == "accepted" else "warning")
            result_channel = await core.resolve_channel(ACCEPTANCE_CHANNEL_ID)
            await result_channel.send(content=f"<@{application['user_id']}>", embed=result_embed, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=application["user_id"])]))
            await notify_applicant(application, f"Staff application {result}", f"Your staff application was **{result}**.\n\nModerator: {moderator}\n\n{instruction}\n\nStaff reason: {reason}")
        await interaction.response.edit_message(embed=application_embed(application), view=None)


async def notify_applicant(application, title, description):
    try:
        user = core.bot.get_user(application["user_id"]) or await core.bot.fetch_user(application["user_id"])
        await user.send(embed=core.make_embed(title, description, "success" if "accepted" in title else "warning"))
    except (discord.Forbidden, discord.NotFound):
        core.app.logger.warning("Could not DM application applicant %s", application["user_id"])


class ApplicationReviewView(discord.ui.View):
    def __init__(self, application_id):
        super().__init__(timeout=None)
        self.application_id = application_id
        self.accept.custom_id = f"staff-application:{application_id}:accept"
        self.reject.custom_id = f"staff-application:{application_id}:reject"
        self.redo.custom_id = f"staff-application:{application_id}:redo"

    @discord.ui.button(label="Accept Application", style=discord.ButtonStyle.success, custom_id="staff-application:accept")
    async def accept(self, interaction, button):
        await self.open_reason(interaction, "accepted")

    @discord.ui.button(label="Reject Application", style=discord.ButtonStyle.danger, custom_id="staff-application:reject")
    async def reject(self, interaction, button):
        await self.open_reason(interaction, "rejected")

    @discord.ui.button(label="Redo", style=discord.ButtonStyle.secondary, custom_id="staff-application:redo")
    async def redo(self, interaction, button):
        await self.open_reason(interaction, "redo")

    async def open_reason(self, interaction, decision):
        if not core.is_designated_staff(interaction.user):
            await core.send_ephemeral_embed(interaction, "Staff access required", "Only administrators and configured staff roles can review applications.", "error")
            return
        await interaction.response.send_modal(DecisionReasonModal(self.application_id, decision))


class Apply(commands.Cog):
    @app_commands.command(name="apply", description="Post the staff application panel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def apply(self, interaction):
        embed = core.make_embed(
            "DonutSMP Staff Applications",
            "We are looking for mature, active, and community-minded members to join the staff team. Select the button below to begin a guided application in your direct messages.",
            "info",
        )
        embed.add_field(name="What to expect", value="Four short sections covering your experience, availability, moderation knowledge, and vision for the community.", inline=False)
        embed.set_footer(text="Please answer honestly and with enough detail for the team to assess your application.")
        channel = await core.resolve_channel(APPLICATION_PANEL_CHANNEL_ID)
        await channel.send(embed=embed, view=StartApplicationView())
        await core.send_ephemeral_embed(interaction, "Application panel posted", f"The staff application panel is now live in {channel.mention}.", "success")


async def setup(bot):
    await bot.add_cog(Apply())
