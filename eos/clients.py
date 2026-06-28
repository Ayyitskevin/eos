"""Client hierarchy — broker → agent (Mise pattern, RE client types)."""

from . import db
from .vocab import STUDIO_ID


def ancestor_ids(client_id: int) -> list[int]:
    rows = db.all_(
        "WITH RECURSIVE sup(id, parent_id, depth) AS ("
        "  SELECT id, parent_id, 0 FROM clients WHERE id=?"
        "  UNION"
        "  SELECT c.id, c.parent_id, sup.depth+1 FROM clients c JOIN sup ON c.id=sup.parent_id"
        ") SELECT id FROM sup WHERE id<>? ORDER BY depth",
        (client_id, client_id),
    )
    return [r["id"] for r in rows]


def descendant_ids(client_id: int) -> list[int]:
    rows = db.all_(
        "WITH RECURSIVE sub(id, depth) AS ("
        "  SELECT id, 0 FROM clients WHERE id=?"
        "  UNION"
        "  SELECT c.id, sub.depth+1 FROM clients c JOIN sub ON c.parent_id=sub.id"
        ") SELECT id FROM sub WHERE id<>? ORDER BY depth, id",
        (client_id, client_id),
    )
    return [r["id"] for r in rows]


def get_client(client_id: int):
    row = db.one("SELECT * FROM clients WHERE id=? AND studio_id=?", (client_id, STUDIO_ID))
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return row


def list_clients():
    return db.all_(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM listings l WHERE l.client_id=c.id) AS n_listings
           FROM clients c WHERE c.studio_id=?
           ORDER BY c.name""",
        (STUDIO_ID,),
    )


def create_client(
    name: str,
    *,
    client_type: str = "agent",
    company: str = "",
    email: str = "",
    phone: str = "",
    license_number: str = "",
    notes: str = "",
    parent_id: int | None = None,
) -> int:
    cid = db.run(
        """INSERT INTO clients
           (studio_id, parent_id, client_type, name, company, email, phone, license_number, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (STUDIO_ID, parent_id, client_type, name.strip(), company.strip(),
         email.strip(), phone.strip(), license_number.strip(), notes.strip()),
    )
    db.audit("admin", "client.create", f"id={cid} name={name.strip()}")
    return cid


def update_client(client_id: int, **fields) -> None:
    allowed = {"parent_id", "client_type", "name", "company", "email",
               "phone", "license_number", "notes"}
    parts = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?")
        params.append(v.strip() if isinstance(v, str) else v)
    if not parts:
        return
    params.append(client_id)
    db.run(f"UPDATE clients SET {', '.join(parts)} WHERE id=?", tuple(params))
    db.audit("admin", "client.update", f"id={client_id}")