"""
Bot de Discord para la liga amateur de League of Legends.

Funcionalidad implementada:
- Comando /crear-partido: crea automáticamente el canal de texto de un
  enfrentamiento entre dos equipos, configura permisos y postea los
  mensajes de reglas/coordinación del partido.

Requisitos previos:
1. Tener Python 3.10+ instalado.
2. Instalar dependencias:  pip install -U discord.py
3. Crear la app/bot en https://discord.com/developers/applications
   - Activar en "Bot" > Privileged Gateway Intents: no hace falta activar
     Message Content Intent para este comando (usamos slash commands).
4. Invitar el bot a tu servidor con permisos: Manage Channels, Send Messages,
   Manage Roles (para poder fijar permisos del canal), Mention Everyone
   (para poder mencionar roles aunque estén configurados como "no mencionable").
5. Guardar el token del bot como variable de entorno DISCORD_TOKEN
   (nunca lo escribas directamente en el código ni lo subas a git).
6. Rellenar los IDs de configuración en la sección CONFIG más abajo.
"""

import csv
import io
import os
import unicodedata
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# CONFIG — rellena esto con los datos de tu servidor
# ---------------------------------------------------------------------------

GUILD_ID = 1547725310508667010          # ID de tu servidor de Discord
CATEGORY_NAME_TEMPLATE = "JORNADA {jornada}"  # Nombre de categoría por jornada, ej. "JORNADA 5"
STAFF_ROLE_ID = 1547729760384319518     # ID del rol de staff/árbitros
ADMIN_ROLE_ID = 1548795786110967919     # ID del rol de admin (ve todos los canales de equipo)
CEO_PREMIER_ROLE_ID = 1548795782360993842   # Ve solo los canales de la división Premier
CEO_ASCEND_ROLE_ID = 1548795784655405087    # Ve solo los canales de la división Ascend
REGLAMENTO_CHANNEL_MENTION = "📜𝗥𝗘𝗚𝗟𝗔𝗠𝗘𝗡𝗧𝗢📜"  # nombre visible del canal de reglas

# --- Vigilancia de tickets sin responder ---
TICKETS_CATEGORY_NAMES = [
    "TICKETS-GENERAL-PREMIER",
    "TICKETS-GENERAL-ASCEND",
    "TICKETS-FICHAJES-PREMIER",
    "TICKETS-FICHAJES-ASCEND",
    "TICKETS-ADMINISTRACION",
]  # Nombres de las categorías donde Ticket Tool crea cada tipo de ticket
TICKET_REVISION_HORAS = 24              # Cada cuántas horas se revisa
TICKET_AVISO_MARCADOR = "⚠️ TICKET_SIN_RESPUESTA"  # Prefijo interno para detectar avisos ya mandados

# Plantillas de los mensajes. Usamos .format() con las variables:
# {equipo1} {equipo2} {jornada} {fecha} {hora}

MENSAJE_1 = """\
**Jornada {jornada} [{fecha} {hora}]** {equipo1} VS {equipo2}

Este canal es la única vía oficial para coordinar vuestro partido. El STAFF se basará exclusivamente en este chat para resolver conflictos; evitad conversaciones privadas.

**ACUERDO DE HORARIO**
Debéis confirmar el horario antes del jueves a las 23:59h. Si no hay acuerdo, el partido se jugará automáticamente en el horario predefinido. Aferrarse a este horario sin negociar NO es una opción.

Durante la fase regular, si acordáis jugar entre lunes y miércoles, no se permiten nuevos registros de jugadores en las 12 horas previas al partido.

**CONVOCATORIA**
Primer mapa. Debéis enviar la alineación (OP.GG), roles (TOP, JGL, MID, ADC, SUPP) y elección de lado (si aplica) por este canal, con al menos 4h de antelación a la hora del partido.

No enviar la convocatoria a tiempo conlleva las siguientes penalizaciones acumulativas:
* (-1 BAN): Si se envía con menos de 4 horas de antelación o se cambian 1-2 jugadores.
* (0 BANS mapa 1): Si se envía con menos de 30 minutos o se cambian 3+ jugadores.
* Abandono: Si no se ha enviado a falta de 5 minutos para el inicio.
"""

MENSAJE_2 = """\
**PREPARACIÓN Y DRAFT**
* Tenéis 10 minutos desde la hora pactada para empezar el draft o se dará el mapa por perdido.
* Las sustituciones y elecciones de lado entre mapas se tienen que comunicar con la mayor brevedad posible.
* Durante TODA la serie (descansos incluidos), los jugadores deberán permanecer en sus respectivos canales de voz. Coachs y suplentes pueden estar en el canal durante los descansos y Draft.
* Se utilizará https://lol.draftcore.net/ en formato Fearless Draft. El STAFF os pasará por este mismo canal el link del draft que debéis usar.
* Es obligatorio usar la función de intercambio en la draftcore para que el orden visual de los campeones coincida exactamente con el de la partida (TOP, JNG, MID, ADC, SUPP).
* En caso de fallo, se coordinará cualquier detalle por este canal y/o se utilizará https://drafter.lol/.

Normas completas y detalladas en {reglamento}. Recordamos que el desconocimiento de estas reglas no exime de su cumplimiento.
"""

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    # Sincroniza los slash commands con tu servidor (rápido, solo ese guild)
    guild = discord.Object(id=GUILD_ID)
    synced = await bot.tree.sync(guild=guild)
    print(f"Conectado como {bot.user}. {len(synced)} comando(s) sincronizado(s).")
    if not revisar_tickets.is_running():
        revisar_tickets.start()


def normalizar_categoria(texto: str) -> str:
    """Convierte tipografías Unicode 'de fantasía' (bold/itálica estilizada) a
    ASCII normal y pasa a minúsculas, para reconocer categorías de tickets
    aunque el nombre visible use una fuente especial."""
    return unicodedata.normalize("NFKD", texto).lower()


def encontrar_categorias_tickets(guild: discord.Guild) -> list[discord.CategoryChannel]:
    objetivos = {normalizar_categoria(n) for n in TICKETS_CATEGORY_NAMES}
    return [c for c in guild.categories if normalizar_categoria(c.name) in objetivos]


@tasks.loop(hours=TICKET_REVISION_HORAS)
async def revisar_tickets():
    """Revisa cada canal de las categorías de tickets (una por cada tipo:
    general premier/ascend, fichajes premier/ascend, administración). Si el
    último mensaje no es de alguien con rol STAFF/ADMIN (ni un aviso que ya
    mandó el propio bot), postea un aviso mencionando a ambos roles."""
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    staff_role = guild.get_role(STAFF_ROLE_ID)
    admin_role = guild.get_role(ADMIN_ROLE_ID)

    categorias = encontrar_categorias_tickets(guild)

    for categoria in categorias:
        for canal in categoria.text_channels:
            try:
                ultimo = [m async for m in canal.history(limit=1)]
            except discord.Forbidden:
                continue  # el bot no tiene acceso a ese canal en concreto

            if not ultimo:
                continue  # canal vacío, nada que avisar

            mensaje = ultimo[0]

            # Evita repetir el aviso si el último mensaje ya es un aviso nuestro
            if mensaje.author == bot.user and TICKET_AVISO_MARCADOR in mensaje.content:
                continue

            autor_es_staff = isinstance(mensaje.author, discord.Member) and (
                (staff_role and staff_role in mensaje.author.roles)
                or (admin_role and admin_role in mensaje.author.roles)
            )

            if not autor_es_staff:
                menciones = " ".join(
                    r.mention for r in (staff_role, admin_role) if r is not None
                )
                await canal.send(
                    f"{TICKET_AVISO_MARCADOR}\n{menciones} — Este ticket lleva más "
                    f"de {TICKET_REVISION_HORAS}h sin respuesta del staff."
                )


@revisar_tickets.before_loop
async def before_revisar_tickets():
    await bot.wait_until_ready()


def slug(role: discord.Role) -> str:
    """Limpia emojis/espacios de un nombre de rol para usarlo como nombre de canal."""
    name = "".join(c for c in role.name if c.isalnum() or c.isspace())
    return name.strip().lower().replace(" ", "-")


def normalizar(texto: str) -> str:
    """Deja solo letras/números/espacios y pasa a minúsculas, para poder comparar
    nombres de equipo aunque el rol tenga emojis, barras u otros símbolos delante
    (ej. '🔴| Planar Shock Pingus' vs 'Planar Shock Pingus' en el CSV)."""
    texto = unicodedata.normalize("NFKD", texto)
    limpio = "".join(c for c in texto if c.isalnum() or c.isspace())
    return " ".join(limpio.lower().split())


def encontrar_rol_por_nombre(guild: discord.Guild, nombre: str) -> discord.Role | None:
    objetivo = normalizar(nombre)
    return discord.utils.find(lambda r: normalizar(r.name) == objetivo, guild.roles)


async def crear_canal_partido(
    guild: discord.Guild,
    staff_role: discord.Role,
    equipo1: discord.Role,
    equipo2: discord.Role,
    jornada: int,
    fecha: str,
    hora: str,
) -> discord.TextChannel:
    """Crea el canal de un enfrentamiento y postea los mensajes de coordinación.
    Reutilizada tanto por /crear-partido como por /crear-jornada."""

    category_name = CATEGORY_NAME_TEMPLATE.format(jornada=jornada)
    category = discord.utils.get(guild.categories, name=category_name)
    if category is None:
        category = await guild.create_category(category_name)

    channel_name = f"{slug(equipo1)}-vs-{slug(equipo2)}"

    # Permisos: oculto por defecto, visible solo para los dos equipos, staff y el bot
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        equipo1: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        equipo2: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
    }

    channel = await guild.create_text_channel(
        channel_name, category=category, overwrites=overwrites
    )

    equipo1_mention = equipo1.mention
    equipo2_mention = equipo2.mention

    msg1 = MENSAJE_1.format(
        jornada=jornada,
        fecha=fecha,
        hora=hora,
        equipo1=equipo1_mention,
        equipo2=equipo2_mention,
    )
    msg2 = MENSAJE_2.format(reglamento=REGLAMENTO_CHANNEL_MENTION)

    await channel.send(
        content=f"{equipo1_mention} {equipo2_mention}",
        embed=discord.Embed(description=msg1, color=discord.Color.blurple()),
    )
    await channel.send(embed=discord.Embed(description=msg2, color=discord.Color.blurple()))

    return channel


def check_staff(interaction: discord.Interaction, staff_role: discord.Role) -> bool:
    return staff_role is not None and staff_role in interaction.user.roles


@bot.tree.command(
    name="crear-jornada",
    description="Crea todos los canales de partido de una jornada a partir de un CSV",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    jornada="Número de jornada",
    archivo="Archivo CSV con columnas: equipo1,equipo2,fecha,hora",
)
async def crear_jornada(
    interaction: discord.Interaction,
    jornada: int,
    archivo: discord.Attachment,
):
    staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
    if not check_staff(interaction, staff_role):
        await interaction.response.send_message(
            "No tienes permiso para usar este comando.", ephemeral=True
        )
        return

    if not archivo.filename.lower().endswith(".csv"):
        await interaction.response.send_message(
            "El archivo debe ser un .csv", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    raw = await archivo.read()
    text = raw.decode("utf-8-sig")  # utf-8-sig tolera el BOM que añade Excel
    reader = csv.DictReader(io.StringIO(text))

    guild = interaction.guild
    creados = []
    errores = []

    for i, fila in enumerate(reader, start=2):  # fila 1 es la cabecera
        try:
            nombre1 = fila["equipo1"].strip()
            nombre2 = fila["equipo2"].strip()
            fecha = fila["fecha"].strip()
            hora = fila["hora"].strip()
        except KeyError:
            errores.append(f"Fila {i}: faltan columnas (equipo1,equipo2,fecha,hora)")
            continue

        rol1 = encontrar_rol_por_nombre(guild, nombre1)
        rol2 = encontrar_rol_por_nombre(guild, nombre2)

        if rol1 is None:
            errores.append(f"Fila {i}: no existe el rol '{nombre1}'")
            continue
        if rol2 is None:
            errores.append(f"Fila {i}: no existe el rol '{nombre2}'")
            continue

        try:
            channel = await crear_canal_partido(
                guild, staff_role, rol1, rol2, jornada, fecha, hora
            )
            creados.append(channel.mention)
        except discord.HTTPException as e:
            errores.append(f"Fila {i} ({nombre1} vs {nombre2}): error de Discord — {e}")

    resumen = f"Creados {len(creados)} canal(es): {', '.join(creados) if creados else '—'}"
    if errores:
        resumen += "\n\n⚠️ Errores:\n" + "\n".join(errores)

    await interaction.followup.send(resumen, ephemeral=True)


@bot.tree.command(
    name="revisar-tickets",
    description="Fuerza ahora mismo la revisión de tickets sin respuesta (sin esperar a las 24h)",
    guild=discord.Object(id=GUILD_ID),
)
async def revisar_tickets_manual(interaction: discord.Interaction):
    staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
    if not check_staff(interaction, staff_role):
        await interaction.response.send_message(
            "No tienes permiso para usar este comando.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    await revisar_tickets()
    await interaction.followup.send("Revisión de tickets ejecutada.", ephemeral=True)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "Falta la variable de entorno DISCORD_TOKEN. "
            "Expórtala antes de ejecutar el bot: export DISCORD_TOKEN=tu_token"
        )
    bot.run(token)