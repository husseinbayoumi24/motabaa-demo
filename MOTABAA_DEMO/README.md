# مُتابع — MOTABAA (نسخة التجربة / DEMO)

**للتجربة فقط:** SQLite محلي + بيانات وهمية. لا تلمس PostgreSQL أبدًا (ترفض التشغيل لو لقت متغير قاعدة PostgreSQL).

## التشغيل
```bash
pip install -r requirements.txt
python seed_demo.py --reset        # يبني demo_data/motabaa_demo.db ببيانات وهمية
streamlit run app.py
```
على ويندوز: دبل كليك على `run_demo.bat`. الحسابات: `DEMO_ACCOUNTS.md` (كلها وهمية، كلمة المرور `Demo@Pass2026`).

ملاحظة: زرار "سجّل حضوري" يتحقق من الـ GPS على السيرفر. نقطة التجربة وهمية (30.0100, 31.2000) فاستخدم
Developer Tools -> Sensors في المتصفح لتحديد الموقع.

الاختبارات: `pytest tests` (قاعدة SQLite مؤقتة).

---
DEMO edition: SQLite + fake data only; never touches PostgreSQL. `python seed_demo.py --reset && streamlit run app.py`.
