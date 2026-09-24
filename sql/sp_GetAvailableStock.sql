-- Stock "disponible" simulado para api-middleware-sap (reemplaza al SP real, que lee SAP).
-- Devuelve la cantidad de dbo.stock_simulado si existe la fila; si no, 1000
-- (mismo valor que DEFAULT_STOCK del simulador del Service Layer).
-- Para probar faltantes de stock: insertar filas en dbo.stock_simulado con cantidad 0.
IF OBJECT_ID(N'dbo.stock_simulado', N'U') IS NULL
    CREATE TABLE dbo.stock_simulado (
        item_code      NVARCHAR(50)   NOT NULL,
        warehouse_code NVARCHAR(20)   NOT NULL,
        cantidad       DECIMAL(19, 6) NOT NULL,
        CONSTRAINT PK_stock_simulado PRIMARY KEY (item_code, warehouse_code)
    );
GO

CREATE OR ALTER PROCEDURE dbo.sp_GetAvailableStock
    @ItemCode      NVARCHAR(50),
    @WarehouseCode NVARCHAR(20)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT COALESCE(
        (SELECT cantidad FROM dbo.stock_simulado
         WHERE item_code = @ItemCode AND warehouse_code = @WarehouseCode),
        1000);
END
GO
