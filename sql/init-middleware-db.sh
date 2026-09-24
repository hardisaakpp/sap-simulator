#!/bin/bash
# Prepara la base sc_middleware de api-middleware-sap en el SQL Server local.
# - Solo si la base NO existe: la crea y ejecuta los scripts de ../api-middleware-sap/sql
#   en su orden histórico (esos scripts no son idempotentes).
# - Siempre: (re)crea el SP simulado sp_GetAvailableStock (idempotente).
set -euo pipefail

SQLCMD=(/opt/mssql-tools18/bin/sqlcmd -S "${DB_HOST}" -U sa -P "${MSSQL_SA_PASSWORD}" -C -b)
DB=sc_middleware

existe=$("${SQLCMD[@]}" -h -1 -W -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM sys.databases WHERE name = '${DB}'" | tr -d '[:space:]')
if [ "${existe}" = "0" ]; then
    echo "Creando base ${DB}..."
    "${SQLCMD[@]}" -Q "CREATE DATABASE ${DB}"
    for script in create_api_log create_job_queue create_pos_sync_status \
                  alter_pos_sync_status_add_doc_type create_api_log_indexes \
                  alter_pos_sync_status_add_doc_num alter_pos_sync_status_add_warehouses; do
        echo "  -> ${script}.sql"
        "${SQLCMD[@]}" -d "${DB}" -i "/middleware-sql/${script}.sql"
    done
else
    echo "La base ${DB} ya existe; no se re-ejecutan los scripts del middleware."
fi

echo "Creando/actualizando sp_GetAvailableStock simulado..."
"${SQLCMD[@]}" -d "${DB}" -i /scripts/sp_GetAvailableStock.sql
echo "sc_middleware lista."
