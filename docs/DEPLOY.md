# Развёртывание на AWS

Один сервер, четыре части: Supabase (база), сервис моделей, API, Telegram-бот.
Ночной прогон — по cron.

## 1. Машина

| Что | Рекомендация | Почему |
|---|---|---|
| Тип | `t3.xlarge` (4 vCPU, 16 ГБ) | Supabase ~3 ГБ + модели ~4 ГБ + запас. На 8 ГБ (`t3.large`) заводится, но впритык |
| Диск | 100 ГБ gp3 | база + веса моделей (~2.5 ГБ) + образы Docker |
| ОС | Ubuntu 24.04 LTS | |
| Порты извне | только 22 (SSH) и 443 | всё остальное — на `127.0.0.1`, наружу не смотрит |
| GPU | не нужен | реранкер и эмбеддинги считаются на процессоре |

Всё, что слушает порты в `docker-compose.yml`, привязано к `127.0.0.1`. Наружу
торчит только то, что ты сам выставишь через nginx.

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 postgresql-client git
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/aladin2907/valencia-info-bot.git && cd valencia-info-bot
```

## 2. Supabase

Ставится отдельно — это самостоятельный стек, мы к нему только подключаемся.

```bash
git clone --depth 1 https://github.com/supabase/supabase supabase-stack
cd supabase-stack/docker
cp .env.example .env
```

В `.env` обязательно поменять: `POSTGRES_PASSWORD`, `JWT_SECRET` (40+ символов),
`ANON_KEY`, `SERVICE_ROLE_KEY`, `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`.
Ключи генерируются на [supabase.com/docs/guides/self-hosting#api-keys](https://supabase.com/docs/guides/self-hosting#api-keys)
из твоего `JWT_SECRET`. Со значениями из примера поднимать нельзя — они публичные.

```bash
docker compose up -d
cd ../..
```

Studio (веб-интерфейс базы) поднимется на порту 8000. Наружу его не открывай —
ходи через SSH-туннель: `ssh -L 8000:localhost:8000 user@server`.

**Зачем Supabase, а не голый Postgres.** Бот ходит в базу напрямую по SQL, ему
хватило бы одного Postgres. Supabase берётся на вырост: мобильному приложению
нужны авторизация и REST, а это в нём уже есть — не придётся писать своё.

## 3. Схема

```bash
cd ~/valencia-info-bot
cp .env.example .env    # заполнить: DATABASE_URL, OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, TG_API_*
./scripts/apply_schema.sh
```

`DATABASE_URL` при Supabase-стеке рядом: `postgresql://postgres:<POSTGRES_PASSWORD>@172.17.0.1:5432/postgres`
(адрес `172.17.0.1` — это хост со стороны контейнера; проверь `docker network inspect bridge`).

Если аутентификация не проходит: в самоподнятом Supabase порт публикует не сам
Postgres, а пулер (supavisor), и имя пользователя там может требовать вид
`postgres.<POOLER_TENANT_ID>` — значение лежит в `supabase-stack/docker/.env`.
Либо опубликуй порт `db` напрямую и ходи в него. Схему и функцию поиска мы
проверили на образе Postgres от Supabase (17.6) — они применяются без правок;
сетевую часть полного стека проверь на месте.

Скрипт идемпотентный, можно гонять повторно — например, после обновления
`sql/002_hybrid_search.sql`.

## 4. Запуск

```bash
docker compose up -d --build
docker compose logs -f models     # первый старт качает веса, ~2.5 ГБ, 5-10 минут
curl -s localhost:8080/health     # {"status":"ok","db":"ok","models":"ok"}
```

Пока `models` не отвечает `ok`, `api` не стартует — это специально: лучше не
подняться, чем отвечать без поиска.

## 5. Данные

Репозиторий приезжает **пустым**: выгрузки чатов — приватная переписка живых
людей, в git их нет. Свежий клон = бот, который на всё отвечает «информация не
найдена». Данные заливает владелец, одним из двух способов.

**Из готовых выгрузок** (быстрее — треды уже собраны):

```bash
scp -r data/ user@server:~/valencia-info-bot/data/
docker compose run --rm ingest python scripts/load_threads.py \
    data/it_ua_valencia/threads.json it_ua_valencia
```

20 633 треда считаются несколько часов на 4 vCPU. Сначала прогони одну группу
с `--limit 500`, убедись что ответы нормальные, потом запускай всё на ночь.

**Из Telegram напрямую** (нужны `TG_API_ID` / `TG_API_HASH` с my.telegram.org и
один раз — вход по номеру телефона):

```bash
docker compose run --rm ingest python -m ingest.nightly --since-days 90
```

Первый вход попросит код из Telegram; сессия ляжет в `sessions/` и переживёт
перезапуск. Нужен обычный аккаунт, а не бот: боты не читают историю групп.

## 6. Ночной прогон

```bash
mkdir -p ~/valencia-info-bot/logs
crontab -e
```

```cron
0 4 * * * cd /home/ubuntu/valencia-info-bot && docker compose run --rm ingest >> logs/ingest.log 2>&1
```

Прогон идемпотентный: упал на середине — следующий запуск догонит, дублей не
будет. Векторы пересчитываются только там, где изменился текст треда.

## 7. Бот

`TELEGRAM_BOT_TOKEN` в `.env` — **тестовый**. Боевой бот сейчас работает на
старом n8n-workflow, и он не трогается, пока новый стек не проверен на живых
вопросах. Порядок переключения, когда дойдут руки: проверить на тестовом →
выключить workflow в n8n → перевести боевой токен. Не наоборот.

## 8. Что дальше

Мобильному приложению отдельный сервер не нужен — оно ходит в тот же
`POST /ask`. Если приложение будет публичным, `api` надо будет закрыть nginx с
TLS и авторизацией Supabase, сейчас он слушает только localhost.
