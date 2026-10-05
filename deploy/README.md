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

Si la web corre en otra máquina y `BRIDGE_HOST` no es `127.0.0.1`, el puerto del bridge (`BRIDGE_PORT`, por defecto `8765`) queda accesible desde la red:

- restringe ese puerto en el cortafuegos para que solo acepte conexiones desde la IP de la máquina de la web;
- cifra el tráfico: el bridge habla `ws://` y el supertoken viaja en claro. Ponlo detrás de un proxy inverso con TLS (`wss://`) o de un túnel cifrado (SSH, WireGuard), y en ese caso deja `BRIDGE_HOST=127.0.0.1`.

Cada `LOGIN` con un supertoken incorrecto cierra la conexión y deja en el journal una línea `WARNING` con la IP de origen (`journalctl -u liga-bot | grep "login fallido"`).

El fichero contiene credenciales: `sudo chown rcl:rcl .env && sudo chmod 600 .env`.

Si la contraseña de PostgreSQL contiene `@ / ? # % :`, codifícalos en
`DATABASE_URL` (`%40 %2F %3F %23 %25 %3A`). Por ejemplo, `pa@ss/1` se escribe
`pa%40ss%2F1`. Sin codificar, la URL se interpreta mal y el bot no puede describirla
en el log (solo muestra `*** [credenciales sin codificar]`).
Para obtener el valor codificado:
`python3 -c "import urllib.parse, getpass; print(urllib.parse.quote(getpass.getpass(), safe=''))"`.

Instala dependencias y aplica migraciones **antes** de arrancar el servicio. El
servicio arranca con `uv run --no-sync` y nunca instala, actualiza ni descarga
paquetes: las dependencias solo se instalan con `uv sync --frozen`. Si el entorno
está incompleto, el bot no arranca, el error queda en `journalctl -u liga-bot` y
systemd lo vuelve a intentar cada 5 s (`Restart=always`), ahora sin acceder a la red,
hasta que ejecutes `uv sync --frozen`.

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

Si adaptas `User`, `WorkingDirectory` o la ruta de uv, conserva las opciones de
`ExecStart` (`run --no-sync liga-bot`).

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

`git pull` no actualiza la unidad instalada en `/etc/systemd/system/`. Si la
versión nueva cambia `deploy/liga-bot.service`, aplica el cambio y recarga systemd
antes de reiniciar. Primero comprueba si la unidad instalada es una copia literal:

```bash
diff deploy/liga-bot.service /etc/systemd/system/liga-bot.service
```

**Copia literal** (sin diferencias salvo el cambio nuevo): vuelve a copiarla.

```bash
sudo cp deploy/liga-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart liga-bot
systemctl cat liga-bot | grep ExecStart   # comprueba la orden en uso
journalctl -u liga-bot -n 50
```

**Unidad adaptada** (`User`, `Group`, `WorkingDirectory`, `HOME` o ruta de uv
propios): no la copies, porque `cp` sustituye esos valores por los del repositorio
y el servicio deja de arrancar. Aplica solo el cambio sobre tu copia. Por ejemplo,
para pasar `ExecStart` a `--no-sync`:

```bash
sudo sed -i 's|run --frozen liga-bot|run --no-sync liga-bot|' /etc/systemd/system/liga-bot.service
# o edítala a mano: sudoedit /etc/systemd/system/liga-bot.service
sudo systemctl daemon-reload
sudo systemctl restart liga-bot
systemctl cat liga-bot | grep ExecStart   # debe mostrar run --no-sync liga-bot
journalctl -u liga-bot -n 50
```

Para que las próximas actualizaciones de la unidad puedan copiarse tal cual, puedes
dejar en `/etc/systemd/system/` la copia literal y guardar las adaptaciones en un
drop-in con `sudo systemctl edit liga-bot` (`User=`, `Group=`, `WorkingDirectory=`,
`Environment=HOME=…` y, si la ruta de uv es otra, `ExecStart=` vacío seguido del
`ExecStart=` completo). El drop-in sobrevive al `cp`, pero un `ExecStart` redefinido
en él tapa los cambios futuros de `ExecStart` del repositorio: revísalo cuando cambie.
