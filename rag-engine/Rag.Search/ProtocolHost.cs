using System.Text.Json;

namespace Rag.Search;

public static class ProtocolHost
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        MaxDepth = 64,
    };

    // One session is strictly sequential. A supplied snapshot always replaces the
    // index, even at the same revision (uncommitted SQLite rollback can reuse it).
    public static void Run(TextReader input, Stream output)
    {
        RetrievalSnapshot? snapshot = null;
        var workspace = new QueryWorkspace();
        using var buffer = new MemoryStream();
        using var writer = new Utf8JsonWriter(buffer);
        string? line;
        while ((line = input.ReadLine()) is not null)
        {
            IReadOnlyList<SearchMatch>? matches = null;
            string? errorCode = null;
            try
            {
                SearchRequest? request = JsonSerializer.Deserialize<SearchRequest>(line, JsonOptions);
                if (request is null || request.Query is null ||
                    (request.MaterialTypes is not null && Array.Exists(request.MaterialTypes, static type => type is null)))
                    errorCode = "invalid_request";
                else
                {
                    if (request.Snapshot is not null) snapshot = RetrievalSnapshot.Create(request.Snapshot);
                    if (snapshot is null) errorCode = "no_snapshot";
                    else matches = workspace.Search(snapshot, request);
                }
            }
            catch (JsonException) { errorCode = "invalid_request"; }
            catch (InvalidSnapshotException) { errorCode = "invalid_snapshot"; }
            catch (Exception) { errorCode = "internal_error"; }

            buffer.SetLength(0);
            writer.Reset(buffer);
            writer.WriteStartObject();
            if (errorCode is not null)
            {
                writer.WriteStartObject("error");
                writer.WriteString("code", errorCode);
                writer.WriteString("message", errorCode switch
                {
                    "invalid_request" => "Request does not match the retrieval protocol.",
                    "invalid_snapshot" => "Snapshot does not match the retrieval protocol.",
                    "no_snapshot" => "A snapshot is required before searching.",
                    _ => "Retrieval failed.",
                });
                writer.WriteEndObject();
            }
            else
            {
                writer.WriteString("revision", snapshot!.Revision);
                writer.WriteStartArray("matches");
                foreach (SearchMatch match in matches!)
                {
                    writer.WriteStartObject();
                    writer.WriteNumber("material_id", match.MaterialId);
                    writer.WriteNumber("chunk_id", match.ChunkId);
                    writer.WriteNumber("score", match.Score);
                    writer.WriteEndObject();
                }
                writer.WriteEndArray();
            }
            writer.WriteEndObject();
            writer.Flush();
            output.Write(buffer.GetBuffer().AsSpan(0, checked((int)buffer.Length)));
            output.WriteByte((byte)'\n');
            output.Flush();
        }
    }
}
