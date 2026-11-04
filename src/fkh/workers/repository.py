"""入库写入仓储。

所有方法都在调用方的事务内执行，自身不提交。入库是一个原子操作：
文档、版本、ACL、切片要么全部可见，要么全部不可见。半成品版本比失败更危险——
它会被后续查询当作真实知识。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from fkh.authz.policy import AclPolicy
from fkh.domain.chunking.types import Chunk
from fkh.domain.lexeme import segment_cjk
from fkh.domain.state_machine import VersionStatus, assert_transition
from fkh.retrieval.embedding import to_pgvector


class DuplicateContentError(ValueError):
    """同一知识文档下已存在相同内容哈希的版本。重复入库通常意味着重复上传。"""

    def __init__(self, version_id: UUID | None = None) -> None:
        if version_id is None:
            message = "同一文档已存在内容完全相同的版本"
        else:
            message = f"同一文档已存在相同内容的版本：{version_id}"
        super().__init__(message)
        self.version_id = version_id


class IngestionRepository:
    def __init__(self, conn: psycopg.Connection[Any], tenant_id: UUID) -> None:
        self._conn = conn
        self._tenant = tenant_id
        # 事务级租户上下文。RLS 依赖它；未设置时任何读写都返回零行，
        # 表现为"写入成功但没有数据"，所以必须在这里一次设好。
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))

    # ---- 文档与版本 --------------------------------------------------------
    def ensure_document(self, *, knowledge_key: str, domain: str, owner_department: str) -> UUID:
        row = self._conn.execute(
            """INSERT INTO knowledge_documents (tenant_id, knowledge_key, domain, owner_department)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, knowledge_key)
               DO UPDATE SET owner_department = EXCLUDED.owner_department
               RETURNING id""",
            (self._tenant, knowledge_key, domain, owner_department),
        ).fetchone()
        if row is None:
            raise RuntimeError("knowledge_documents 写入未返回 id")
        return UUID(str(row[0]))

    def next_version_no(self, document_id: UUID) -> int:
        row = self._conn.execute(
            """SELECT coalesce(max(version_no), 0) + 1
                 FROM knowledge_versions
                WHERE tenant_id = %s AND document_id = %s""",
            (self._tenant, document_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("版本号查询失败")
        return int(row[0])

    def find_version_by_content(self, document_id: UUID, content_sha256: str) -> UUID | None:
        row = self._conn.execute(
            """SELECT id FROM knowledge_versions
                WHERE tenant_id = %s AND document_id = %s AND content_sha256 = %s
                LIMIT 1""",
            (self._tenant, document_id, content_sha256),
        ).fetchone()
        return UUID(str(row[0])) if row is not None else None

    def create_version(
        self,
        *,
        document_id: UUID,
        version_no: int,
        title: str,
        classification: int,
        audience: str,
        required_scope: str,
        content_sha256: str,
        source_object_key: str,
    ) -> UUID:
        try:
            # 用 savepoint 包住插入：唯一约束冲突后当前语句所属事务会被中止，
            # 没有 savepoint 就无法再查出"已存在的那个版本号"来给出可读错误。
            with self._conn.transaction():
                row = self._conn.execute(
                    """INSERT INTO knowledge_versions
                         (tenant_id, document_id, version_no, title, classification, audience,
                          required_scope, status, content_sha256, source_object_key)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self._tenant,
                        document_id,
                        version_no,
                        title,
                        classification,
                        audience,
                        required_scope,
                        VersionStatus.RAW.value,
                        content_sha256,
                        source_object_key,
                    ),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            existing = self.find_version_by_content(document_id, content_sha256)
            raise DuplicateContentError(existing) from exc

        if row is None:
            raise RuntimeError("knowledge_versions 写入未返回 id")
        return UUID(str(row[0]))

    def transition(
        self,
        *,
        version_id: UUID,
        source: VersionStatus,
        target: VersionStatus,
        actor_id: UUID,
        reason: str | None = None,
    ) -> None:
        action = assert_transition(source, target)
        if target is VersionStatus.DRAFT:
            # 退回草稿必须同时清空审批人。数据库触发器会拒绝"DRAFT 且带审批人"的组合，
            # 因为那意味着一个未审的草稿看起来已经被批准过。
            self._conn.execute(
                """UPDATE knowledge_versions
                      SET status = %s, approved_by = NULL, public_approved_by = NULL
                    WHERE tenant_id = %s AND id = %s""",
                (target.value, self._tenant, version_id),
            )
        else:
            self._conn.execute(
                "UPDATE knowledge_versions SET status = %s WHERE tenant_id = %s AND id = %s",
                (target.value, self._tenant, version_id),
            )
        self._conn.execute(
            """INSERT INTO review_actions (tenant_id, version_id, actor_id, action, reason)
               VALUES (%s, %s, %s, %s, %s)""",
            (self._tenant, version_id, actor_id, action.value, reason),
        )

    # ---- ACL ---------------------------------------------------------------
    def create_acl(self, policy: AclPolicy) -> UUID:
        row = self._conn.execute(
            "INSERT INTO acl_policies (tenant_id, policy_json) VALUES (%s, %s) RETURNING id",
            (self._tenant, Jsonb(policy.to_json())),
        ).fetchone()
        if row is None:
            raise RuntimeError("acl_policies 写入未返回 id")
        return UUID(str(row[0]))

    # ---- 切片 --------------------------------------------------------------
    def next_index_generation(self) -> int:
        """分配新的索引代号。

        用事务级 advisory lock 串行化同一租户的分配，避免并发入库拿到同一个代号。
        跨租户不需要互斥：generation 只在租户内比较。
        """
        self._conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(self._tenant),))
        row = self._conn.execute(
            "SELECT coalesce(max(index_generation), 0) + 1 FROM knowledge_chunks WHERE tenant_id = %s",
            (self._tenant,),
        ).fetchone()
        if row is None:
            raise RuntimeError("index_generation 分配失败")
        return int(row[0])

    def insert_chunks(
        self,
        *,
        version_id: UUID,
        parents: Sequence[Chunk],
        children: Sequence[Chunk],
        acl_policy_id: UUID,
        index_generation: int,
        tokenizer_id: str,
        lexeme_tokenizer_id: str,
        parser_id: str,
        language: str,
        embeddings: Sequence[Sequence[float]] | None = None,
        embedding_model_id: str | None = None,
    ) -> tuple[int, int]:
        expected = len(parents) + len(children)
        if embeddings is not None:
            if len(embeddings) != expected:
                raise ValueError(f"向量数量 {len(embeddings)} 与切片数量 {expected} 不一致")
            if embedding_model_id is None:
                raise ValueError("写入向量必须同时给出 embedding_model_id")

        cursor = 0

        # 父块必须先落库：chunk 的父引用有外键，且触发器要求父块已存在且为 parent 类型
        parent_ids: dict[int, UUID] = {}
        for chunk in parents:
            parent_ids[chunk.ordinal] = self._insert_chunk(
                version_id=version_id,
                chunk=chunk,
                parent_id=None,
                acl_policy_id=acl_policy_id,
                index_generation=index_generation,
                tokenizer_id=tokenizer_id,
                lexeme_tokenizer_id=lexeme_tokenizer_id,
                parser_id=parser_id,
                language=language,
                embedding=embeddings[cursor] if embeddings is not None else None,
                embedding_model_id=embedding_model_id,
            )
            cursor += 1

        for chunk in children:
            parent_id: UUID | None = None
            if chunk.parent_ordinal is not None:
                parent_id = parent_ids.get(chunk.parent_ordinal)
                if parent_id is None:
                    raise ValueError(
                        f"child#{chunk.ordinal} 指向不存在的 parent#{chunk.parent_ordinal}"
                    )
            self._insert_chunk(
                version_id=version_id,
                chunk=chunk,
                parent_id=parent_id,
                acl_policy_id=acl_policy_id,
                index_generation=index_generation,
                tokenizer_id=tokenizer_id,
                lexeme_tokenizer_id=lexeme_tokenizer_id,
                parser_id=parser_id,
                language=language,
                embedding=embeddings[cursor] if embeddings is not None else None,
                embedding_model_id=embedding_model_id,
            )
            cursor += 1

        return len(parents), len(children)

    def _insert_chunk(
        self,
        *,
        version_id: UUID,
        chunk: Chunk,
        parent_id: UUID | None,
        acl_policy_id: UUID,
        index_generation: int,
        tokenizer_id: str,
        lexeme_tokenizer_id: str,
        parser_id: str,
        language: str,
        embedding: Sequence[float] | None = None,
        embedding_model_id: str | None = None,
    ) -> UUID:
        row = self._conn.execute(
            """INSERT INTO knowledge_chunks
                 (tenant_id, version_id, parent_chunk_id, chunk_kind, chunk_text, chunk_sha256,
                  tokenizer_id, lexeme_tokenizer_id, heading_path, locator, metadata,
                  acl_policy_id, index_generation, lexeme, embedding, embedding_model_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       to_tsvector('simple', %s), %s::vector, %s)
               RETURNING id""",
            (
                self._tenant,
                version_id,
                parent_id,
                chunk.kind.value,
                chunk.text,
                chunk.content_sha256,
                tokenizer_id,
                lexeme_tokenizer_id,
                list(chunk.heading_path),
                Jsonb(chunk.locator.to_json()),
                Jsonb(
                    {
                        "parser_id": parser_id,
                        "language": language,
                        "table_index": chunk.table_index,
                        "token_count": chunk.token_count,
                    }
                ),
                acl_policy_id,
                index_generation,
                segment_cjk(chunk.text),
                to_pgvector(embedding) if embedding is not None else None,
                embedding_model_id,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("knowledge_chunks 写入未返回 id")
        return UUID(str(row[0]))
