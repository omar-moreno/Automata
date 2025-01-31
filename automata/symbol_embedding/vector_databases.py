from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, List, Optional, TypeVar

import numpy as np

from automata.core.base.database.vector import ChromaVectorDatabase, JSONVectorDatabase
from automata.symbol.parser import parse_symbol
from automata.symbol_embedding.base import SymbolEmbedding

if TYPE_CHECKING:
    from chromadb.api.types import GetResult

V = TypeVar("V", bound=SymbolEmbedding)


class ChromaSymbolEmbeddingVectorDatabase(ChromaVectorDatabase[str, V]):
    """Concrete class to provide a vector database that saves into a Chroma db."""

    def __init__(
        self,
        collection_name: str,
        factory: Callable[..., V],
        persist_directory: Optional[str] = None,
    ):
        super().__init__(collection_name, persist_directory)
        self._factory = factory

    def entry_to_key(self, entry: V) -> str:
        """
        Generates a simple hashable key from a Symbol.
        """
        return entry.symbol.dotpath

    def add(self, entry: V) -> None:
        """
        Adds a SymbolEmbedding to the collection.

        Adds the specified SymbolEmbedding to the collection.
        FIXME - Adding raw symbol to the metadata is a hack
        to get around the fact that we are using 'dotpaths' as keys
        rather than the raw symbols.
        We should think of a smarter way to approach this problem.
        We have chosen to use dotpaths since they are easier to maintain
        as a commit hash change will not cause them to become stale.
        """
        metadata = deepcopy(entry.metadata)
        metadata["symbol_uri"] = entry.symbol.uri
        self._collection.add(
            documents=[entry.document],
            metadatas=[metadata],
            ids=[self.entry_to_key(entry)],
            embeddings=[[int(ele) for ele in entry.vector]],
        )

    def get(
        self,
        key: str,
        *args: Any,
        **kwargs: Any,
    ) -> V:
        """
        Retrieves an entry from the collection using the provided key.

        Keyword Args:
            ids: The ids of the embeddings to get. Optional.
            where: A Where type dict used to filter results by.
                E.g. `{"color" : "red", "price": 4.20}`. Optional.
            limit: The number of documents to return. Optional.
            offset: The offset to start returning results from.
                    Useful for paging results with limit. Optional.
            where_document: A WhereDocument type dict used to filter by the documents.
                            E.g. `{$contains: {"text": "hello"}}`. Optional.
            include: A list of what to include in the results.
                    Can contain `"embeddings"`, `"metadatas"`, `"documents"`.
                    Ids are always included.
                    Defaults to `["metadatas", "documents", "embeddings"]`. Optional.
        """
        kwargs = {
            "ids": key,
            "where": kwargs.get("where"),
            "limit": kwargs.get("limit"),
            "offset": kwargs.get("offset"),
            "where_document": kwargs.get("where_document"),
            "include": kwargs.get("include", ["documents", "metadatas", "embeddings"]),
        }

        result = self._collection.get(**kwargs)
        if len(result["ids"]) == 0:
            raise KeyError(f"Get failed with {key} not in database")
        elif len(result["ids"]) > 1:
            raise KeyError(f"Get failed with {key}, multiple entries found")

        return self._construct_object_from_result(result)

    def discard(self, key: str, **kwargs: Any) -> None:
        """Deletes an entry from the collection using the provided key."""

        self._collection.delete(ids=[key])

    def clear(self):
        """Clears all entries in the collection."""
        self._collection.delete(where={})

    def contains(self, key: str) -> bool:
        """Checks if a key is present in the collection."""
        result = self._collection.get(ids=[key])
        return len(result["ids"]) != 0

    def get_ordered_embeddings(self) -> List[V]:
        """Retrieves all embeddings in the collection in a sorted order."""
        results = self._collection.get(include=["documents", "metadatas", "embeddings"])
        embeddings = [
            self._construct_object_from_result(
                {"metadatas": [metadata], "documents": [document], "embeddings": [embedding]}
            )
            for metadata, document, embedding in zip(
                results["metadatas"], results["documents"], results["embeddings"]
            )
        ]
        return sorted(embeddings, key=lambda x: x.symbol.dotpath)

    def load(self) -> None:
        # As Chroma is a live database, no specific load action is required.
        pass

    def save(self) -> None:
        # As Chroma is a live database, no specific save action is required.
        pass

    def update_database(self, entry: V):
        """Updates an entry in the database."""
        # Update the entry in the database.
        metadata = deepcopy(entry.metadata)
        metadata["symbol_uri"] = entry.symbol.uri
        self._collection.update(
            documents=[entry.document],
            metadatas=[metadata],
            ids=[self.entry_to_key(entry)],
            embeddings=[[int(ele) for ele in entry.vector]],
        )

    def _construct_object_from_result(self, result: "GetResult") -> V:
        """Constructs an object from the provided result."""
        metadatas = result["metadatas"][0]
        metadatas["key"] = parse_symbol(metadatas.pop("symbol_uri"))
        metadatas["vector"] = np.array(result["embeddings"][0]).astype(int)
        metadatas["document"] = result["documents"][0]

        return self._factory(**metadatas)


class JSONSymbolEmbeddingVectorDatabase(JSONVectorDatabase[str, SymbolEmbedding]):
    """Concrete class to provide a vector database that saves into a JSON file."""

    def __init__(self, file_path: str):
        super().__init__(file_path)

    def entry_to_key(self, entry: SymbolEmbedding) -> str:
        """Concrete implementation to generate a simple hashable key from a Symbol."""
        return entry.symbol.dotpath

    def get_ordered_embeddings(self) -> List[SymbolEmbedding]:
        return sorted(self.data, key=lambda x: self.entry_to_key(x))
