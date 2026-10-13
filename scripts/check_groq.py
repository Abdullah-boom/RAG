"""M0 hello-world: list Groq models that exist for this key and make one call."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from groq import Groq  # noqa: E402

from app import config  # noqa: E402

client = Groq(api_key=config.GROQ_API_KEY)
print("Models:", ", ".join(sorted(m.id for m in client.models.list().data)))
r = client.chat.completions.create(
    model=config.GROQ_MODEL, temperature=0,
    messages=[{"role": "user", "content": "Say hello in English and Urdu, one line."}],
)
print(f"{config.GROQ_MODEL}:", r.choices[0].message.content)
