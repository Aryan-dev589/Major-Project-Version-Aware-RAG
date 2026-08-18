"""rag/llm/prompt_builder.py"""

SYSTEM_PROMPT = """You are an HR Policy Assistant for an enterprise organisation.

STRICT RULES:
1. Answer ONLY using the policy excerpts provided below.
2. If the answer is not in the excerpts, say exactly: "I couldn't find this information in the available policies."
3. NEVER invent, assume, or extrapolate information.
4. Always cite the policy name, version, and section in your answer.
5. Be concise and professional.
6. If multiple policies apply, mention all of them.
7. If policies conflict, highlight the conflict clearly."""


def build_prompt(query: str, chunks: list[dict], chat_history: list[dict] = None) -> list[dict]:
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        # Bulletproof extraction: look for a nested 'metadata' dict, fallback to the chunk itself
        meta = chunk.get("metadata", chunk)
        
        context_parts.append(
            f"[Excerpt {i}]\n"
            f"Policy: {meta.get('policy_name', 'Unknown')} | "
            f"Version: {meta.get('version', 'N/A')} | "
            f"Section: {meta.get('section', 'General')} | "
            f"Page: {meta.get('page', 'N/A')}\n"
            f"{chunk.get('text', '')}"
        )
    context = "\n\n---\n\n".join(context_parts)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if chat_history:
        for msg in chat_history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({
        "role": "user",
        "content": f"POLICY EXCERPTS:\n\n{context}\n\nQUESTION: {query}\n\nAnswer based only on the excerpts above."
    })
    
    return messages