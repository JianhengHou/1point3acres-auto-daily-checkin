from src.config import Settings
from src.client import P3ASession
from src.quiz import answer_today
from src.signer import sign_today

cfg = Settings()                        # captcha_key 放 config.toml 或 Secret
sess = P3ASession(cfg)
sess.login()

errors = []
try:
    print(sign_today(
        sess,
        mood="yl",
        text="每日打卡 https://github.com/HelinXu/1point3acres-auto-daily-checkin",
        captcha_api_key=cfg.captcha_key,
    ))
except Exception as exc:
    errors.append(f"签到失败: {exc}")
    print(errors[-1])

try:
    print(answer_today(sess, captcha_api_key=cfg.captcha_key))
except Exception as exc:
    errors.append(f"答题失败: {exc}")
    print(errors[-1])

if errors:
    raise SystemExit(1)
