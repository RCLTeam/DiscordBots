# Despliegue de LigaBot

Servicio systemd que mantiene el bot siempre activo y lo reinicia si se cae.

## 1. Preparar el servidor

```bash
# Usuario sin privilegios que ejecutará el bot
sudo useradd --system --create-home --shell /usr/sbin/nologin rcl

# Código del bot
sudo mkdir -p /opt/rcl
sudo git clone https://github.com/RCLTeam/DiscordBots.git /opt/rcl/discord-bots
sudo chown -R rcl:rcl /opt/rcl/discord-bots

# uv (si no está instalado): instálalo en una ruta accesible por el servicio
curl -LsSf https://astral.sh/uv/install.sh | sudo sh
which uv   # confirma la ruta y ajústala en ExecStart
```

## 2. Configurar el entorno

Crea `/opt/rcl/discord-bots/.env` a partir de `.env.example`. Como mínimo:

```env
DISCORD_TOKEN=...
GUILD_ID=...
DATABASE_URL=postgresql+asyncpg://rcl_user:PASSWORD@localhost:5432/rcl
DISCORD_BOT_SUPERTOKEN=...        # el mismo valor que usa la web
SUGGESTIONS_CHANNEL_ID=...        # si queda en 0, no se publican sugerencias
BRIDGE_HOST=127.0.0.1             # 0.0.0.0 solo si la web corre en otra máquina
```

El fichero contiene credenciales: `sudo chown rcl:rcl .env && sudo chmod 600 .env`.

Instala dependencias y aplica migraciones **antes** de arrancar el servicio:

```bash
cd /opt/rcl/discord-bots
sudo -u rcl uv sync --frozen
sudo -u rcl uv run alembic upgrade head
```

## 3. Instalar el servicio

```bash
sudo cp deploy/liga-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liga-bot
```

## 4. Operación

```bash
systemctl status liga-bot          # estado
journalctl -u liga-bot -f          # logs en vivo
journalctl -u liga-bot -n 100      # últimas 100 líneas
sudo systemctl restart liga-bot    # reiniciar
sudo systemctl stop liga-bot       # parar
```

Tras arrancar por primera vez, registra los slash commands escribiendo `!sync`
en cualquier canal del servidor de Discord.

### Credenciales y logs

Al arrancar, el bot registra qué motor de base de datos inicializa con la URL
saneada (motor, host, puerto y base de datos, por ejemplo
`PostgreSQL (postgresql+asyncpg://localhost:5432/rcl)`); nunca el usuario, la
contraseña ni los parámetros de `DATABASE_URL`. Tampoco aparecen en los errores
de una `DATABASE_URL` mal escrita.

Las versiones anteriores escribían `DATABASE_URL` completa en el journal en cada
arranque. Si el servidor ejecutó alguna de ellas, la contraseña sigue en los logs
antiguos: cámbiala y actualiza `.env`.

```bash
sudo -u postgres psql -c '\password rcl_user'      # pide la contraseña sin mostrarla
sudoedit /opt/rcl/discord-bots/.env      # actualiza DATABASE_URL
sudo systemctl restart liga-bot
```

Revisa también quién puede leer el journal (`root` y los grupos `adm` y
`systemd-journal`): `getent group adm systemd-journal`.

## 5. Actualizar a una versión nueva

```bash
cd /opt/rcl/discord-bots
sudo -u rcl git pull
sudo -u rcl uv sync --frozen
sudo -u rcl uv run alembic upgrade head
sudo systemctl restart liga-bot
```
