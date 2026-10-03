import re

import discord
from discord import app_commands
from discord.ext import commands

import main as core


URL_PATTERN = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
DEFAULT_COLOR = 0x4866E0


def parse_color(value):
    value = (value or "").strip().lstrip("#")
    if not value:
        return DEFAULT_COLOR
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError("Color must be a six-digit hex value such as `4866E0` or `#4866E0`.")
    return int(value, 16)


def validate_url(value, label):
    value = (value or "").strip()
    if value and not URL_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a valid http(s) URL.")
    return value or None


def build_embed(title, description, color, footer, image_url, thumbnail_url, fields):
    embed = discord.Embed(title=title, description=description, color=discord.Color(color))
    if footer:
        embed.set_footer(text=footer)
    if image_url:
        embed.set_image(url=image_url)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    return embed


class FieldModal(discord.ui.Modal, title="Add embed field"):
    name = discord.ui.TextInput(label="Field name", max_length=256)
    value = discord.ui.TextInput(label="Field value", style=discord.TextStyle.paragraph, max_length=1024)
    inline = discord.ui.TextInput(label="Inline? Type Yes or No", default="Yes", min_length=2, max_length=3)

    def __init__(self, builder):
        super().__init__()
        self.builder = builder

    async def on_submit(self, interaction):
        inline = str(self.inline.value).strip().casefold()
        if inline not in {"yes", "no"}:
            await core.send_ephemeral_embed(interaction, "Invalid inline value", "Type exactly **Yes** or **No**.", "warning")
            return
        if len(self.builder.fields) >= 25:
            await core.send_ephemeral_embed(interaction, "Field limit reached", "Discord embeds support up to 25 fields.", "warning")
            return
        self.builder.fields.append((str(self.name.value).strip(), str(self.value.value).strip(), inline == "yes"))
        await interaction.response.edit_message(embed=self.builder.preview(), view=self.builder)


class EmbedBuilderView(discord.ui.View):
    def __init__(self, channel, title, description, color, footer, image_url, thumbnail_url):
        super().__init__(timeout=900)
        self.channel = channel
        self.title_text = title
        self.description_text = description
        self.color = color
        self.footer = footer
        self.image_url = image_url
        self.thumbnail_url = thumbnail_url
        self.fields = []

    def preview(self):
        embed = build_embed(
            self.title_text,
            self.description_text,
            self.color,
            self.footer,
            self.image_url,
            self.thumbnail_url,
            self.fields,
        )
        embed.set_author(name=core.BOT_NAME)
        embed.set_footer(text=f"Preview • {self.footer}" if self.footer else "Preview • DonutSMP Essentials Assistant")
        return embed

    @discord.ui.button(label="➕ Add field", style=discord.ButtonStyle.primary, row=0)
    async def add_field(self, interaction, button):
        await interaction.response.send_modal(FieldModal(self))

    @discord.ui.button(label="✏️ Edit details", style=discord.ButtonStyle.secondary, row=1)
    async def edit_details(self, interaction, button):
        await interaction.response.send_modal(EditEmbedModal(self))

    @discord.ui.button(label="📤 Publish", style=discord.ButtonStyle.success, row=0)
    async def publish(self, interaction, button):
        await self.channel.send(
            embed=build_embed(self.title_text, self.description_text, self.color, self.footer, self.image_url, self.thumbnail_url, self.fields),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await core.send_audit_event(interaction.guild, "custom_embed_published", interaction.user, self.channel, f"Published a custom embed in {self.channel.mention}.")
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"✅ Published in {self.channel.mention}.", embed=self.preview(), view=self)

    @discord.ui.button(label="✖ Cancel", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction, button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Embed builder cancelled.", view=self)


class EditEmbedModal(discord.ui.Modal, title="Edit embed details"):
    title_text = discord.ui.TextInput(label="Title", max_length=256)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=4000)
    color = discord.ui.TextInput(label="Color hex", placeholder="#4866E0", max_length=7)
    footer = discord.ui.TextInput(label="Footer text", required=False, max_length=2048)
    image_url = discord.ui.TextInput(label="Image URL", required=False, max_length=500)

    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.title_text.default = builder.title_text
        self.description.default = builder.description_text
        self.color.default = f"#{builder.color:06X}"
        self.footer.default = builder.footer
        self.image_url.default = builder.image_url or ""

    async def on_submit(self, interaction):
        try:
            color = parse_color(str(self.color.value))
            image_url = validate_url(str(self.image_url.value), "Image URL")
        except ValueError as error:
            await core.send_ephemeral_embed(interaction, "Embed details need attention", str(error), "warning")
            return
        self.builder.title_text = str(self.title_text.value).strip()
        self.builder.description_text = str(self.description.value).strip()
        self.builder.color = color
        self.builder.footer = str(self.footer.value).strip()
        self.builder.image_url = image_url
        await interaction.response.edit_message(embed=self.builder.preview(), view=self.builder)


class EmbedModal(discord.ui.Modal, title="Create a custom embed"):
    title_text = discord.ui.TextInput(label="Title", max_length=256)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=4000)
    color = discord.ui.TextInput(label="Color hex", placeholder="#4866E0", required=False, max_length=7)
    footer = discord.ui.TextInput(label="Footer text", required=False, max_length=2048)
    image_url = discord.ui.TextInput(label="Image URL", required=False, max_length=500)

    def __init__(self, channel, thumbnail_url=None):
        super().__init__()
        self.channel = channel
        self.thumbnail_url = thumbnail_url

    async def on_submit(self, interaction):
        title = str(self.title_text.value).strip()
        description = str(self.description.value).strip()
        if not title or not description:
            await core.send_ephemeral_embed(
                interaction,
                "Missing embed content",
                "Title and description cannot be blank.",
                "warning",
            )
            return
        try:
            color = parse_color(str(self.color.value))
            image_url = validate_url(str(self.image_url.value), "Image URL")
            thumbnail_url = validate_url(self.thumbnail_url, "Thumbnail URL")
        except ValueError as error:
            await core.send_ephemeral_embed(interaction, "Embed details need attention", str(error), "warning")
            return
        builder = EmbedBuilderView(
            self.channel,
            title,
            description,
            color,
            str(self.footer.value).strip(),
            image_url,
            thumbnail_url,
        )
        await interaction.response.send_message(
            content=f"Preview for {self.channel.mention}. Add fields or publish when ready.",
            embed=builder.preview(),
            view=builder,
            ephemeral=True,
        )


class EmbedBuilder(commands.GroupCog, group_name="embed", group_description="Create polished custom embeds"):
    @app_commands.command(name="create", description="Build and publish a custom embed with fields and styling.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Channel where the final embed will be published",
        thumbnail_url="Optional thumbnail URL; use https:// or http://",
    )
    async def create(self, interaction: discord.Interaction, channel: discord.TextChannel = None, thumbnail_url: str = None):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await core.send_ephemeral_embed(interaction, "Invalid channel", "Choose a standard text channel.", "warning")
            return
        try:
            thumbnail_url = validate_url(thumbnail_url, "Thumbnail URL")
        except ValueError as error:
            await core.send_ephemeral_embed(interaction, "Invalid thumbnail", str(error), "warning")
            return
        await interaction.response.send_modal(EmbedModal(channel, thumbnail_url))


async def setup(bot):
    await bot.add_cog(EmbedBuilder())
