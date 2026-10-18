/// <summary>Не удалось прочитать {Path}.</summary>
/// <remarks>
/// Подробная документация для <see cref="Path"/> сохраняется.
/// </remarks>
public sealed class Sample
{
    public void Run(ILogger logger, string path, int count)
    {
        logger.LogError("Не удалось загрузить {Path}", path);
        logger.LogWarning($"Готово: {count}");
        logger.LogDebug($"Повторите попытку: {GetCount(path):N0}");
        logger.LogInformation("""Не найдено""");
        logger.LogDebug($"""Состояние: {GetState(path)}""");
        logger.LogDebug(@"Путь C:\temp следует сохранить");
        logger.LogDebug(@"Путь ""{Path}"" следует сохранить");
        logger.LogInformation("""Проверьте состояние {Path}""");
        var protocol = @"ACK";
        Assert.Equal("mumu:7", id.ToString());
        throw new InvalidOperationException("Не удалось завершить операцию.");
        _ = protocol;
    }
}
