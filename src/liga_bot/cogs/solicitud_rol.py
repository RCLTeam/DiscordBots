"""
Módulo de solicitud de rol de equipo — RCL Bot

Flujo jugador:
1. Al entrar, se le asigna automáticamente el rol SIN_VERIFICAR_ROLE_ID.
2. Usa /solicitar-rol en #pedir-rol. Se abre un modal con: nombre de
   invocador de LoL y Riot Tag (para una futura verificación con la API
   de Riot). El equipo NO se pide en el modal — Discord no permite
   menús desplegables dentro de un modal, así que se pregunta justo
   después con un select menu.
3. Elige su equipo en el desplegable (20 equipos + "Jugador Libre").
   - Si elige un equipo normal: se crea un ticket privado (canal de
     texto) visible solo para el usuario y STAFF/ADMIN/CEO/CEO
     Premier/CEO Ascend, con los datos rellenados.
   - Si elige "Jugador Libre": no se crea ticket. Se le asigna el rol
     Jugador Libre y se le quita Sin Verificar al momento, sin revisión
     manual (no pertenece a ningún equipo, no hay nada que verificar).

Flujo staff:
- En el ticket, revisan los datos y usan /asignar-rol para dar el rol
  de equipo correcto al usuario y quitarle Sin Verificar en el mismo
  paso. Después cierran el ticket con el botón, que borra el canal.

Integración: importar `setup_solicitud_rol(bot)` y llamarlo una vez
desde tu archivo principal (liga_bot.py), después de crear `bot`.
"""

import re
import unicodedata

import discord
from discord import app_commands
from discord.ext import commands

# ── CONFIGURA ESTOS VALORES ──────────────────────────────────────────
GUILD_ID = 1547725310508667010
STAFF_ROLE_ID = 1547729760384319518
ADMIN_ROLE_ID = 1548795786110967919
CEO_PREMIER_ROLE_ID = 1548795782360993842
CEO_ASCEND_ROLE_ID = 1548795784655405087
CEO_ROLE_ID = 1548795781174009977

SIN_VERIFICAR_ROLE_ID = 1550466708593180682
TICKET_ROL_CATEGORY_ID = 1550476204014969022

FREE_ROLE_NAME = "Libre"  # debe coincidir con el nombre real del rol en Discord

TEAMS_PREMIER = [
    "Planar Shock Pingus", "Fnix Esports", "Rift Maligators", "VyronX Panda",
    "Draconis Aeterni", "Tilt Masters", "Troncos", "Dive. Flip. Repeat.",
    "Nova+", "Power Team",
]
TEAMS_ASCEND = [
    "Akelarre", "Akelarre Exsules", "Draconis Aeterni Academy", "La Divina Papaya",
    "Lotus", "Palitos", "Pingus Academy", "Ramitas", "Rift Pups", "The Lost Guardian",
]
TEAMS_ALL = TEAMS_PREMIER + TEAMS_ASCEND  # 20 equipos, usado en los dos comandos
# ──────────────────────────────────────────────────────────────────────

STAFF_ROLE_IDS = [
    STAFF_ROLE_ID,
    ADMIN_ROLE_ID,
    CEO_ROLE_ID,
    CEO_PREMIER_ROLE_ID,
    CEO_ASCEND_ROLE_ID,
]


def normalizar(texto: str) -> str:
    """Deja solo letras/números/espacios y pasa a minúsculas, para poder
    comparar nombres de equipo aunque el rol real tenga emojis, barras u
    otros símbolos delante en Discord."""
    texto = unicodedata.normalize("NFKD", texto)
    limpio = "".join(c for c in texto if c.isalnum() or c.isspace())
    return " ".join(limpio.lower().split())


def buscar_rol_por_nombre(guild: discord.Guild, nombre: str) -> discord.Role | None:
    objetivo = normalizar(nombre)
    return discord.utils.find(lambda r: normalizar(r.name) == objetivo, guild.roles)


def es_staff(member: discord.Member) -> bool:
    return any(role.id in STAFF_ROLE_IDS for role in member.roles)


# ---------------------------------------------------------------------------
# Paso 1: modal con nombre de invocador + Riot Tag
# ---------------------------------------------------------------------------

class SolicitudRolModal(discord.ui.Modal, title="Solicitar rol de equipo"):
    nombre_lol = discord.ui.TextInput(
        label="Nombre de invocador (LoL)",
        placeholder="Ej: MiInvocador123",
        max_length=50,
        required=True,
    )
    riot_tag = discord.ui.TextInput(
        label="Riot Tag",
        placeholder="Ej: EUW1 o AB12 (letras y/o números)",
        max_length=20,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Paso 2: pedir el equipo con un desplegable (esto no cabe en el modal)
        await interaction.response.send_message(
            "Ahora selecciona tu equipo:",
            view=EquipoSelectView(str(self.nombre_lol), str(self.riot_tag)),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Paso 2: desplegable de equipo (20 equipos + Jugador Libre)
# ---------------------------------------------------------------------------

class EquipoSelect(discord.ui.Select):
    def __init__(self, nombre_lol: str, riot_tag: str):
        self.nombre_lol = nombre_lol
        self.riot_tag = riot_tag

        options = [discord.SelectOption(label=equipo) for equipo in TEAMS_ALL]
        options.append(discord.SelectOption(label=FREE_ROLE_NAME, emoji="🕊️"))

        super().__init__(
            placeholder="Elige tu equipo...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        equipo_elegido = self.values[0]
        guild = interaction.guild
        miembro = interaction.user

        if equipo_elegido == FREE_ROLE_NAME:
            await self._asignar_jugador_libre(interaction, guild, miembro)
        else:
            await self._crear_ticket(interaction, guild, miembro, equipo_elegido)

    async def _asignar_jugador_libre(
        self, interaction: discord.Interaction, guild: discord.Guild, miembro: discord.Member
    ):
        rol_libre = buscar_rol_por_nombre(guild, FREE_ROLE_NAME)
        if rol_libre is None:
            await interaction.response.edit_message(
                content=f"No encuentro el rol '{FREE_ROLE_NAME}' en el servidor. Avisa a un admin.",
                view=None,
            )
            return

        await miembro.add_roles(rol_libre, reason="Se registró como Jugador Libre")

        rol_no_verificado = guild.get_role(SIN_VERIFICAR_ROLE_ID)
        if rol_no_verificado and rol_no_verificado in miembro.roles:
            await miembro.remove_roles(rol_no_verificado, reason="Verificado como Jugador Libre")

        await interaction.response.edit_message(
            content=f"Rol {rol_libre.mention} asignado. Ya tienes acceso a los canales correspondientes.",
            view=None,
        )

    async def _crear_ticket(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        miembro: discord.Member,
        equipo_elegido: str,
    ):
        categoria = guild.get_channel(TICKET_ROL_CATEGORY_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            miembro: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, embed_links=True
            ),
        }
        for role_id in STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        nombre_canal = f"rol-{miembro.name}"[:90]
        canal = await guild.create_text_channel(
            name=nombre_canal,
            category=categoria,
            overwrites=overwrites,
            reason=f"Solicitud de rol de equipo de {miembro}",
        )

        embed = discord.Embed(
            title="Nueva solicitud de rol de equipo",
            color=discord.Color.purple(),
        )
        embed.add_field(name="Usuario", value=miembro.mention, inline=False)
        embed.add_field(name="Nombre de invocador", value=self.nombre_lol, inline=False)
        embed.add_field(name="Riot Tag", value=self.riot_tag, inline=False)
        embed.add_field(name="Equipo solicitado", value=equipo_elegido, inline=False)
        embed.set_footer(
            text="Verifica al jugador y usa /asignar-rol. Luego cierra el ticket."
        )

        vista = TicketView()
        vista.add_item(ConfirmarRolButton(miembro.id, equipo_elegido))

        await canal.send(
            content=" ".join(
                f"<@&{rid}>" for rid in STAFF_ROLE_IDS if guild.get_role(rid)
            ),
            embed=embed,
            view=vista,
        )

        await interaction.response.edit_message(
            content=f"Solicitud enviada. Tu ticket es {canal.mention}, espera a que el staff lo revise.",
            view=None,
        )


class EquipoSelectView(discord.ui.View):
    def __init__(self, nombre_lol: str, riot_tag: str):
        super().__init__(timeout=300)
        self.add_item(EquipoSelect(nombre_lol, riot_tag))


class PanelPedirRolView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Solicitar mi rol",
        style=discord.ButtonStyle.primary,
        emoji="🎮",
        custom_id="panel_pedir_rol_boton",
    )
    async def solicitar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SolicitudRolModal())


# ---------------------------------------------------------------------------
# Botones del ticket: Confirmar Rol (asigna automático) y Denegar Rol (borra)
# ---------------------------------------------------------------------------

class ConfirmarRolButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"confirmar_rol:(?P<user_id>[0-9]+):(?P<equipo>.+)",
):
    """Botón cuyo custom_id lleva codificado el usuario y el equipo que
    pidió, así que sigue funcionando después de reiniciar el bot sin
    depender de que la vista original siga viva en memoria."""

    def __init__(self, user_id: int, equipo: str):
        super().__init__(
            discord.ui.Button(
                label="Confirmar Rol",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"confirmar_rol:{user_id}:{equipo}",
            )
        )
        self.user_id = user_id
        self.equipo = equipo

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["user_id"]), match["equipo"])

    async def callback(self, interaction: discord.Interaction):
        if not es_staff(interaction.user):
            await interaction.response.send_message(
                "No tienes permiso para usar este botón.", ephemeral=True
            )
            return

        guild = interaction.guild
        usuario = guild.get_member(self.user_id)
        if usuario is None:
            await interaction.response.send_message(
                "No encuentro a ese usuario en el servidor (¿habrá salido?).",
                ephemeral=True,
            )
            return

        rol_equipo = buscar_rol_por_nombre(guild, self.equipo)
        if rol_equipo is None:
            await interaction.response.send_message(
                f"No encuentro el rol '{self.equipo}' en el servidor.", ephemeral=True
            )
            return

        await usuario.add_roles(rol_equipo, reason=f"Rol confirmado por {interaction.user}")

        rol_no_verificado = guild.get_role(SIN_VERIFICAR_ROLE_ID)
        if rol_no_verificado and rol_no_verificado in usuario.roles:
            await usuario.remove_roles(rol_no_verificado, reason="Rol de equipo confirmado")

        await interaction.response.send_message(
            f"Rol {rol_equipo.mention} asignado a {usuario.mention}. Cerrando ticket..."
        )
        await interaction.channel.delete(reason=f"Rol confirmado por {interaction.user}")


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Denegar Rol",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="denegar_rol_ticket",
    )
    async def denegar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not es_staff(interaction.user):
            await interaction.response.send_message(
                "Solo el staff puede cerrar este ticket.", ephemeral=True
            )
            return

        await interaction.response.send_message("Denegando rol y cerrando ticket...")
        await interaction.channel.delete(reason=f"Rol denegado por {interaction.user}")


# ---------------------------------------------------------------------------
# Registro de comandos y eventos
# ---------------------------------------------------------------------------

def setup_solicitud_rol(bot: commands.Bot):
    """Llama a esto una vez desde tu liga_bot.py, después de crear `bot`."""

    @bot.event
    async def on_member_join(member: discord.Member):
        if member.guild.id != GUILD_ID:
            return
        rol = member.guild.get_role(SIN_VERIFICAR_ROLE_ID)
        if rol:
            await member.add_roles(rol, reason="Rol asignado automáticamente al entrar")

    @bot.tree.command(
        name="pedir-rol",
        description="Solicita tu rol de equipo en la liga",
        guild=discord.Object(id=GUILD_ID),
    )
    async def pedir_rol(interaction: discord.Interaction):
        await interaction.response.send_modal(SolicitudRolModal())

    @bot.tree.command(
        name="asignar-rol",
        description="Asigna el rol de un equipo a un usuario y le quita Sin Verificar",
        guild=discord.Object(id=GUILD_ID),
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        equipo="Equipo al que se le va a asignar el usuario",
        usuario="Usuario que solicitó el rol (menciónalo o escribe su nombre)",
    )
    @app_commands.choices(
        equipo=[app_commands.Choice(name=t, value=t) for t in TEAMS_ALL]
    )
    async def asignar_rol(
        interaction: discord.Interaction,
        equipo: app_commands.Choice[str],
        usuario: discord.Member,
    ):
        if not es_staff(interaction.user):
            await interaction.response.send_message(
                "No tienes permiso para usar este comando.", ephemeral=True
            )
            return

        rol_equipo = buscar_rol_por_nombre(interaction.guild, equipo.value)
        if rol_equipo is None:
            await interaction.response.send_message(
                f"No encuentro el rol '{equipo.value}' en el servidor.", ephemeral=True
            )
            return

        await usuario.add_roles(rol_equipo, reason=f"Asignado por {interaction.user}")

        rol_no_verificado = interaction.guild.get_role(SIN_VERIFICAR_ROLE_ID)
        if rol_no_verificado and rol_no_verificado in usuario.roles:
            await usuario.remove_roles(rol_no_verificado, reason="Rol de equipo asignado")

        await interaction.response.send_message(
            f"Rol {rol_equipo.mention} asignado a {usuario.mention} y Sin Verificar retirado.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="publicar-panel-rol",
        description="Publica en este canal el panel con el botón para pedir rol",
        guild=discord.Object(id=GUILD_ID),
    )
    @app_commands.default_permissions(manage_guild=True)
    async def publicar_panel_rol(interaction: discord.Interaction):
        if not es_staff(interaction.user):
            await interaction.response.send_message(
                "No tienes permiso para usar este comando.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔥 Únete a la Rebel Crown Legacy",
            description=(
                "Este es el paso previo a formar parte de la liga. Pulsa el botón "
                "de abajo para dejarnos tu **nombre de invocador** y tu **Riot Tag**, "
                "y a continuación elige el equipo con el que compites.\n\n"
                "🛡️ **¿Tienes equipo?** Se abrirá un ticket privado solo visible para "
                "ti y el staff, donde verificaremos tus datos y te asignaremos el rol.\n\n"
                "🕊️ **¿Vas por libre?** Selecciona **Libre** en el desplegable — no hace "
                "falta ticket ni espera, se te asigna el rol al momento y ya tendrás "
                "acceso a los canales correspondientes.\n\n"
                "Sin este paso no podrás ver el resto del servidor, así que no tardes."
            ),
            color=discord.Color.from_str("#a24bff"),
        )
        embed.set_footer(text="RCL · Rebel Crown Legacy")

        await interaction.channel.send(embed=embed, view=PanelPedirRolView())
        await interaction.response.send_message("Panel publicado.", ephemeral=True)

    # Registra las vistas con custom_id persistente para que los botones
    # sigan funcionando aunque el bot se reinicie.
    bot.add_view(PanelPedirRolView())
    bot.add_view(TicketView())
    bot.add_dynamic_items(ConfirmarRolButton)