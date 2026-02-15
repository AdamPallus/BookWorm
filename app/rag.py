from typing import List, Dict
from openai import OpenAI

client = OpenAI()

SYSTEM_PROMPT = """
You are a spoiler-free book companion. Answer ONLY using the provided excerpts.
Do NOT use any knowledge about this book from your training data.
If the excerpts don't contain the answer, say you don't have enough information.
Cite the excerpts by chapter and position. Keep the answer concise.
""".strip()


def embed_texts(texts: List[str]) -> List[List[float]]:
  resp = client.embeddings.create(
    model="text-embedding-3-small",
    input=texts,
  )
  return [d.embedding for d in resp.data]


def answer_question(question: str, excerpts: List[Dict]) -> Dict:
  context_lines = []
  for ex in excerpts:
    context_lines.append(
      f"[Chapter {ex['chapter_index'] + 1} | Pos {ex['position_index']}]\n{ex['text']}"
    )

  context = "\n\n".join(context_lines)

  messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": f"Question: {question}\n\nExcerpts:\n{context}"},
  ]

  resp = client.chat.completions.create(
    model="gpt-5.1-mini",
    messages=messages,
    temperature=0.2,
  )

  answer = resp.choices[0].message.content
  return {"answer": answer}
