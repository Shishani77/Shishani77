# @cuefa_robot — «Камень, Ножницы, Бумага» (inline) на aiogram 3

Запуск: `pip install -r requirements.txt && python bot.py`
Админка: команда `/admin` в ЛС с ботом (только ID из `ADMIN_IDS`).
Настройки оформления хранятся в `settings.json`, пользователи и счётчик игр — в `bot.db`.

## Настройка `.env` и Inline Mode

### 1. Файл `.env`
1. `cp .env.example .env`
2. В [@BotFather](https://t.me/BotFather): `/mybots` → `@cuefa_robot` → **API Token** — скопируйте токен.
3. Заполните `.env`:
   ```
   BOT_TOKEN=<ваш_токен>
   ADMIN_IDS=<ваш_id>,<id_второго_админа>
   ```
   Свой ID можно узнать у [@userinfobot](https://t.me/userinfobot).
4. `.env` не коммитьте (уже в `.gitignore`).

### 2. Включение Inline Mode в BotFather
1. `/mybots` → `@cuefa_robot` → **Bot Settings** → **Inline Mode** → **Turn on**
   (или `/setinline`).
2. Введите placeholder, например: `Вызвать на дуэль`.
3. Проверка: в любом чате наберите `@cuefa_robot` — появится карточка «Вызвать на дуэль».

> Рассылка доходит только до тех, кто хоть раз написал боту в ЛС (`/start`); остальным Telegram не даёт писать первыми.
