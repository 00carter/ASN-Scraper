# ASN-Scraper

## Ranges

| Name  | FIRST   | LAST    |
| ----- | ------- | ------- |
| Aly   | 0       | 80,000  |
| Mahy  | 80,000  | 160,000 |
| Peter | 160,000 | 240,000 |
| Sama  | 240,000 | 320,000 |
| Shahd | 320,000 | 400,000 |

## How to run?

1. `pip install -r requirements.txt`
2. Open `scraper.py` and set your range at the top. For example, Aly:

```python
   FIRST = 0
   LAST = 80000
```

2. Run it:

```bash
   playwright install chromium
   python scraper.py
```
