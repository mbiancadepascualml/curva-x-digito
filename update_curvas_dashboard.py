#!/usr/bin/env python3
"""
Auto-updater for curvas_vencimiento Grid dashboard.
Schedule: daily at 8am ARG (11am UTC)  —  doc: 01KYMG7HR4FM3DNWABA6BM0QXN
"""

import gzip, json, os, re, zipfile, tempfile
import requests
from google.cloud import bigquery

GRID_DOC_ID = "01KYMG7HR4FM3DNWABA6BM0QXN"
GRID_API    = "https://grid.melioffice.com/api/v1"
SKILL_VER   = "3.6.5"
BQ_PROJECT  = "meli-bi-data"

client = bigquery.Client(project=BQ_PROJECT)

# ── Queries ──────────────────────────────────────────────────────────────────

QUERY_CURVAS = """
SELECT
    A.SIT_SITE_ID,
    CAST(FORMAT_DATE('%Y%m', A.MONTH_ID) AS INT64)       AS M,
    A.CRD_CREDIT_TYPE                                     AS CT,
    A.FECHA_VENCIMIENTO_TEORICA                           AS FVT,
    COALESCE(B.DIAS_DESPLAZAMIENTO, 0)                    AS DESP,
    A.ULTIMO_DIGITO                                       AS UD,
    TRIM(COALESCE(A.COL_LAST_CALL_CENTER_ASSIGNED, ''))   AS AG,
    A.DIAS_DESDE_VENCIMIENTO                              AS D,
    SUM(A.REMAINING_CAPITAL_NUMERADOR)                    AS N,
    SUM(A.REMAINING_CAPITAL_DENOMINADOR)                  AS DN
FROM `meli-bi-data.SBOX_COLLECTIONSDA.CREDIST_CURVAS_BIANCA` A
LEFT JOIN `meli-bi-data.SBOX_COLLECTIONSDA.DESPLAZADOS_CURVAS` B
    ON  A.SIT_SITE_ID               = B.SIT_SITE_ID
    AND A.CRD_CREDIT_TYPE           = B.CRD_CREDIT_TYPE
    AND A.FECHA_VENCIMIENTO_TEORICA = B.FECHA_VENCIMIENTO_TEORICA
    AND A.MONTH_ID                  = B.MONTH_ID
WHERE (A.DIAS_DESDE_VENCIMIENTO = 0
    OR A.DIAS_DESDE_VENCIMIENTO <= DATE_DIFF(
        DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY),
        DATE_ADD(A.MONTH_ID, INTERVAL (A.FECHA_VENCIMIENTO_TEORICA + COALESCE(B.DIAS_DESPLAZAMIENTO, 0) - 1) DAY),
        DAY))
GROUP BY ALL
"""

QUERY_CE = """
WITH COL_CREDITS_AGREEMENTS AS (
    SELECT DISTINCT
        SAFE_CAST(FORMAT_DATE('%Y%m', A.CRD_AGR_INST_PAID_DATE_ID) AS INT64) AS COL_MONTH_ID,
        B.CUS_CUST_ID_BORROWER,
        1 AS FLAG_PARCELAMENTO
    FROM `meli-bi-data.WHOWNER.BT_MP_CREDITS_DEBT_AGREEMENTS_INSTALLMENT` A
    LEFT JOIN `meli-bi-data.WHOWNER.BT_MP_CREDITS_DEBT_AGREEMENTS` B
        ON A.CRD_AGREEMENT_ID = B.CRD_AGREEMENT_ID
    WHERE SAFE_CAST(FORMAT_DATE('%Y%m', A.CRD_AGR_INST_PAID_DATE_ID) AS INT64) >=
          SAFE_CAST(FORMAT_DATE('%Y%m', LAST_DAY(DATE_SUB(DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY), INTERVAL 24 MONTH))) AS INT64)
      AND CRD_AGR_INST_PAID_DATE_ID IS NOT NULL
),
COL_CREDITS_DATAMART AS (
    SELECT
        A.COL_MONTH_ID, A.CUS_CUST_ID_BORROWER, A.SIT_SITE_ID,
        A.CRD_CREDIT_TYPE, A.CRD_CREDIT_SUBTYPE,
        D.CRD_PRODUCT_ID_DEF,
        SUM(COL_TOTAL_DEBT_AMT_PAID_LC  - COL_LATE_FEE_DEBT_AMT_PAID_LC  * IFNULL(B.IVA_VALUE, 1.0)) AS RECUPERO,
        SUM(COL_WORST_TOTAL_DEBT_AMT_LC - COL_WORST_LATE_FEE_DEBT_AMT_LC * IFNULL(B.IVA_VALUE, 1.0)) AS DEUDA_VENCIDA,
        MAX(COL_WORST_DPD)               AS CRD_DIAS_ATRASO_WORST,
        SUM(COL_TOTAL_DEBT_AMT_PAID_LC)  AS RECUPERO_DEUDA_Y_PUNITORIOS
    FROM `meli-bi-data.WHOWNER.BT_COL_CREDITS_DEBT_DATAMART` A
    LEFT JOIN `SBOX_COLLECTIONSDA.AUXILIAR_IVA` B ON A.SIT_SITE_ID = B.SIT_SITE_ID
    LEFT JOIN `SBOX_COLLECTIONSDA.COL_TABLEAU_MAIN_KPIS_DIMENSION_PRODUCT` D
        ON A.CRD_CREDIT_ID = D.CRD_CREDIT_ID
    WHERE A.COL_MONTH_ID >= SAFE_CAST(FORMAT_DATE('%Y%m',
          LAST_DAY(DATE_SUB(DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY), INTERVAL 24 MONTH))) AS INT64)
    GROUP BY 1, 2, 3, 4, 5, 6
),
COL_QUALIFY_PRODUCT AS (
    SELECT A.CUS_CUST_ID_BORROWER, A.COL_MONTH_ID, A.CRD_CREDIT_TYPE,
           A.CRD_CREDIT_SUBTYPE, A.SIT_SITE_ID, A.CRD_DIAS_ATRASO_WORST
    FROM COL_CREDITS_DATAMART A
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY A.COL_MONTH_ID, A.CUS_CUST_ID_BORROWER
        ORDER BY CRD_DIAS_ATRASO_WORST DESC
    ) = 1
),
COL_FX AS (
    SELECT SIT_SITE_ID,
           SAFE_CAST(FORMAT_DATE('%Y%m', TIM_DAY) AS INT64) AS TIM_MONTH,
           CCO_TC_VALUE
    FROM `meli-bi-data.WHOWNER.LK_CURRENCY_CONVERTION`
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY SIT_SITE_ID, SAFE_CAST(FORMAT_DATE('%Y%m', TIM_DAY) AS INT64)
        ORDER BY TIM_DAY DESC
    ) = 1
)
SELECT
    A.COL_MONTH_ID                                                   AS m,
    A.SIT_SITE_ID                                                    AS s,
    CAST(RIGHT(CAST(A.CUS_CUST_ID_BORROWER AS STRING), 1) AS INT64) AS ud,
    CASE
        WHEN D.CRD_DIAS_ATRASO_WORST < 3                             THEN '< 3'
        WHEN D.CRD_DIAS_ATRASO_WORST BETWEEN 3   AND 30             THEN '03 - 30'
        WHEN D.CRD_DIAS_ATRASO_WORST BETWEEN 31  AND 60             THEN '31 - 60'
        WHEN D.CRD_DIAS_ATRASO_WORST BETWEEN 61  AND 90             THEN '61 - 90'
        WHEN D.CRD_DIAS_ATRASO_WORST BETWEEN 91  AND 180            THEN '91 - 180'
        WHEN D.CRD_DIAS_ATRASO_WORST BETWEEN 181 AND 360            THEN '181 - 360'
        WHEN D.CRD_DIAS_ATRASO_WORST > 360                          THEN '> 361'
    END                                                              AS b,
    UPPER(D.CRD_CREDIT_SUBTYPE)                                     AS cst,
    ROUND(SUM(SAFE_DIVIDE(
        CASE WHEN C.FLAG_PARCELAMENTO = 1 THEN A.RECUPERO_DEUDA_Y_PUNITORIOS ELSE A.RECUPERO END,
        FX.CCO_TC_VALUE)), 2)                                        AS rec,
    ROUND(SUM(SAFE_DIVIDE(
        CASE WHEN C.FLAG_PARCELAMENTO = 1 THEN A.RECUPERO_DEUDA_Y_PUNITORIOS ELSE A.DEUDA_VENCIDA END,
        FX.CCO_TC_VALUE)), 2)                                        AS deu
FROM COL_CREDITS_DATAMART A
LEFT JOIN  COL_CREDITS_AGREEMENTS C
    ON  A.CUS_CUST_ID_BORROWER = C.CUS_CUST_ID_BORROWER
    AND A.COL_MONTH_ID         = C.COL_MONTH_ID
INNER JOIN COL_QUALIFY_PRODUCT D
    ON  A.CUS_CUST_ID_BORROWER = D.CUS_CUST_ID_BORROWER
    AND A.COL_MONTH_ID         = D.COL_MONTH_ID
LEFT JOIN  COL_FX FX
    ON  A.SIT_SITE_ID  = FX.SIT_SITE_ID
    AND A.COL_MONTH_ID = FX.TIM_MONTH
WHERE D.CRD_CREDIT_TYPE IS NOT NULL
  AND D.CRD_CREDIT_SUBTYPE IS NOT NULL
  AND A.COL_MONTH_ID >= 202602
  AND UPPER(D.CRD_CREDIT_TYPE) = 'CONSUMER'
GROUP BY 1, 2, 3, 4, 5
"""

# ── Data processing ───────────────────────────────────────────────────────────

def make_idx(values):
    unique = sorted(set(v for v in values if v is not None))
    return {v: i for i, v in enumerate(unique)}, unique


def build_curvas_files(rows):
    """Converts BQ rows → (data_a.js bytes, data_b.js bytes, dicts dict)."""
    s_idx,  s_list  = make_idx(r.SIT_SITE_ID for r in rows)
    ct_idx, ct_list = make_idx(r.CT for r in rows)
    ag_idx, ag_list = make_idx(r.AG for r in rows)

    encoded = []
    for r in rows:
        m_val = r.M
        if hasattr(m_val, 'strftime'):          # DATE type → YYYYMM int
            m_val = int(m_val.strftime('%Y%m'))
        encoded.append([
            s_idx[r.SIT_SITE_ID],
            ct_idx[r.CT],
            int(r.FVT  or 0),
            int(r.DESP or 0),
            int(r.UD   or 0),
            ag_idx.get(r.AG, 0),
            int(m_val),
            int(r.D),
            int((r.N  or 0) / 1000),   # en miles
            int((r.DN or 0) / 1000),
        ])

    mid    = len(encoded) // 2
    data_a = gzip.compress(json.dumps({"rows": encoded[:mid]}, separators=(',', ':')).encode())
    data_b = gzip.compress(json.dumps({"rows": encoded[mid:]}, separators=(',', ':')).encode())
    dicts  = {"s": s_list, "ct": ct_list, "ag": ag_list}
    return data_a, data_b, dicts


def build_ce_data(rows):
    """Formats CE BQ rows as CE_DATA JSON objects."""
    return [
        {
            "m":   int(r.m),
            "s":   r.s,
            "ud":  int(r.ud  or 0),
            "b":   r.b,
            "cst": r.cst or "",
            "rec": float(r.rec or 0),
            "deu": float(r.deu or 0),
        }
        for r in rows
        if r.b is not None
    ]


def inject_ce_data(html, ce_data):
    """Replaces the inline CE_DATA array in the HTML."""
    ce_json = json.dumps(ce_data, separators=(',', ':'), ensure_ascii=False)
    return re.sub(
        r'const CE_DATA=\[.*?\];',
        f'const CE_DATA={ce_json};',
        html,
        flags=re.DOTALL,
    )

# ── Grid helpers ──────────────────────────────────────────────────────────────

def grid_get(path):
    r = requests.get(f"{GRID_API}{path}")
    r.raise_for_status()
    return r.json()


def grid_engine(payload):
    r = requests.post(f"{GRID_API}/engine/run/json", json=payload)
    r.raise_for_status()
    return r.json()


def upload_zip(zip_path):
    """3-step presigned upload (Flow B — new version of existing doc)."""
    file_size = os.path.getsize(zip_path)

    # Step 1 — reserve slot
    resp = grid_engine({
        "skill_version": SKILL_VER,
        "doc_id": GRID_DOC_ID,
        "presigned_upload": {
            "filename":     "curvas_vencimiento.zip",
            "content_type": "application/zip",
            "file_size":    file_size,
        },
    })
    assert resp.get("steps", [{}])[-1].get("label") == "presigned_upload_ready", \
        f"Step 1 failed: {resp}"
    slot_id    = resp["doc_id"]
    upload_url = resp["data"]["upload_url"]

    # Step 2 — PUT file
    with open(zip_path, "rb") as f:
        r = requests.put(upload_url, data=f, headers={"Content-Type": "application/zip"})
    assert r.status_code == 204, f"PUT failed with {r.status_code}"

    # Step 3 — confirm
    resp = grid_engine({
        "skill_version":   SKILL_VER,
        "doc_id":          slot_id,
        "confirm_presigned": True,
        "file_size":       file_size,
    })
    assert resp.get("ok"), f"Step 3 failed: {resp}"
    print(f"  Uploaded → v{resp['version']}: {resp['view_url']}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("[1/5] Querying curvas data from BQ...")
    curvas_rows = list(client.query(QUERY_CURVAS).result())
    print(f"      {len(curvas_rows):,} rows")

    print("[2/5] Querying CE data from BQ...")
    ce_rows = list(client.query(QUERY_CE).result())
    print(f"      {len(ce_rows):,} rows")

    print("[3/5] Processing curvas data...")
    data_a, data_b, dicts = build_curvas_files(curvas_rows)
    print(f"      data_a: {len(data_a)/1e6:.1f} MB  data_b: {len(data_b)/1e6:.1f} MB")

    print("[4/5] Processing CE data and updating HTML...")
    ce_data = build_ce_data(ce_rows)

    dl   = grid_get(f"/documents/{GRID_DOC_ID}/download")
    r    = requests.get(dl["agent_download_url"])
    r.raise_for_status()

    with tempfile.TemporaryDirectory() as tmp:
        zip_in = os.path.join(tmp, "in.zip")
        with open(zip_in, "wb") as f:
            f.write(r.content)

        with zipfile.ZipFile(zip_in) as z:
            html = z.read("curvas_vencimiento.html").decode("utf-8")

        html = inject_ce_data(html, ce_data)

        zip_out = os.path.join(tmp, "curvas_vencimiento.zip")
        with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_STORED) as z:
            z.writestr("curvas_vencimiento.html", html.encode("utf-8"))
            z.writestr("data_a.js",  data_a)
            z.writestr("data_b.js",  data_b)
            z.writestr("dicts.json", json.dumps(dicts, separators=(',', ':')))

        print("[5/5] Uploading new version to Grid...")
        upload_zip(zip_out)

    print("Done!")


if __name__ == "__main__":
    main()
