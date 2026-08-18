from rag.vectordb.chroma import _get_collection

# Grab the raw ChromaDB collection directly
col = _get_collection()

# Perform the query
results = col.query(
    query_texts=["What is the password policy?"],
    n_results=5
)

print("\n--- TOP CHUNKS IN VECTOR DB ---")
if results and "documents" in results and results["documents"]:
    for i, doc in enumerate(results["documents"][0]):
        print(f"\nChunk {i+1}:")
        print(doc)
        print("-" * 30)
else:
    print("No chunks found in database.")