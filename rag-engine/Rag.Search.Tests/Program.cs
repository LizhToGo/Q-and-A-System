using System.Text;
using System.Text.Json;
using Rag.Search;

internal static class Program
{
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    private static int assertions;

    private static int Main()
    {
        ScoringAndChunkSelection();
        ChineseTermsAndNonOverlap();
        RankingDeduplicationAndRevocation();
        ExactFiltersAndLimits();
        UnicodeTokenBoundaries();
        SnapshotTransitionsAndProtocolErrors();
        DeterministicCorpusCases();
        Console.WriteLine($"PASS: {assertions} consumer assertions");
        return 0;
    }

    private static void ScoringAndChunkSelection()
    {
        var material = M(1, title: "Alpha alpha", chunks: [C(12, 2, "ALPHA alpha alpha"), C(11, 1, "alpha")]);
        JsonElement[] replies = Exchange(Q("ALPHA alpha", [material]), Q("alpha beta"), Q("missing"));
        Matches(replies[0], (1, 11, 6)); // Combined shared/chunk cap, repeated query deduplicated.
        Matches(replies[1], (1, 11, 6)); // Recall unions terms: absent beta must not suppress alpha.
        Matches(replies[2]);
        Matches(Exchange(Q("aaaa", [M(1, content: "aaaaa", chunks: [C(11, 0, "aaaaaaaaa")])]))[0], (1, 11, 6));
        Matches(Exchange(Q("alpha", [M(1, tags: ["ALPHA"], section: "alpha", chunks: [C(11, 0, "")])]))[0], (1, 11, 4));
        Matches(Exchange(Q("alpha", [M(1, title: "alpha", chunks: [])]))[0]);
        Matches(Exchange(Q("alpha", [M(1, chunks: [C(12, 0, "alpha"), C(11, 0, "alpha")])]))[0], (1, 11, 2));
        Matches(Exchange(Q("alpha", [M(1, chunks: [C(11, 0, "alpha"), C(12, 8, "alpha alpha")])]))[0], (1, 12, 4));
        // Bigram postings can recall a false positive, but exact scoring must reject it.
        Matches(Exchange(Q("abcd", [M(1, chunks: [C(11, 0, "ab cd bc")])]))[0]);
    }

    private static void ChineseTermsAndNonOverlap()
    {
        var materials = new[]
        {
            M(1, chunks: [C(11, 0, "连续函数")]),
            M(2, chunks: [C(21, 0, "连续 连续")]),
            M(3, chunks: [C(31, 0, "函数")]),
        };
        Matches(Exchange(Q("连续函数", materials))[0], (1, 11, 12), (2, 21, 4), (3, 31, 2));
        Matches(Exchange(Q("哈哈哈 哈哈哈", [M(1, chunks: [C(11, 0, "哈哈哈哈哈")])]))[0], (1, 11, 6));
        Matches(Exchange(Q("函 x 1 !", materials))[0]);
    }

    private static void RankingDeduplicationAndRevocation()
    {
        var materials = new[]
        {
            M(1, verified: false, anchor: "page-a", chunks: [C(11, 0, "alpha")]),
            M(2, verified: true, anchor: "page-a", chunks: [C(21, 0, "alpha")]),
            M(3, verified: true, anchor: "page-b", chunks: [C(31, 0, "alpha alpha")]),
            M(4, verified: true, revoked: true, anchor: "page-b", chunks: [C(41, 0, "alpha alpha alpha")]),
            M(5, verified: false, chunks: [C(51, 0, "alpha")]),
            M(6, verified: true, anchor: "page-c", chunks: [C(61, 0, "alpha")]),
        };
        Matches(Exchange(Q("alpha", materials))[0], (3, 31, 4), (2, 21, 2), (6, 61, 2), (5, 51, 2));
        // Verification is a tie breaker, not a score boost; whitespace anchors are not empty.
        Matches(Exchange(Q("alpha", [M(1, verified: false, anchor: " ", chunks: [C(11, 0, "alpha alpha")]),
            M(2, verified: true, chunks: [C(21, 0, "alpha")])]))[0], (1, 11, 4), (2, 21, 2));
        // A real source anchor may intentionally collide with another material's fallback.
        Matches(Exchange(Q("alpha", [M(1, chunks: [C(11, 0, "alpha")]),
            M(2, anchor: "material:1", chunks: [C(21, 0, "alpha")])]))[0], (1, 11, 2));
    }

    private static void ExactFiltersAndLimits()
    {
        var materials = new[]
        {
            M(1, course: 1, topic: 1, document: 1, type: "definition", chunks: [C(11, 0, "alpha")]),
            M(2, course: 2, topic: 1, document: 2, type: "example", chunks: [C(21, 0, "alpha")]),
            M(3, course: 1, topic: null, document: null, type: "example", chunks: [C(31, 0, "alpha")]),
        };
        JsonElement[] replies = Exchange(Q("alpha", materials), Q("alpha", course: 2), Q("alpha", topic: 1),
            Q("alpha", document: 2), Q("alpha", types: ["example"], ids: [3]),
            Q("alpha", course: 1, topic: 1, document: 1, types: ["definition"], ids: [1]),
            Q("alpha", types: [], ids: []), Q("alpha", limit: 1), Q("alpha", limit: 0),
            Q("alpha", limit: -1), Q("alpha", limit: -3), Q("alpha", limit: int.MinValue),
            Q("alpha", types: ["Definition"]), Q("alpha", course: 0), Q("alpha", ids: [99]));
        Matches(replies[0], (1, 11, 2), (2, 21, 2), (3, 31, 2));
        Matches(replies[1], (2, 21, 2));
        Matches(replies[2], (1, 11, 2), (2, 21, 2));
        Matches(replies[3], (2, 21, 2));
        Matches(replies[4], (3, 31, 2));
        Matches(replies[5], (1, 11, 2));
        Matches(replies[6], (1, 11, 2), (2, 21, 2), (3, 31, 2));
        Matches(replies[7], (1, 11, 2));
        Matches(replies[8]);
        Matches(replies[9], (1, 11, 2), (2, 21, 2));
        for (int i = 10; i < replies.Length; i++) Matches(replies[i]);
        Matches(Exchange(Q("alpha", [M(1, anchor: "same", chunks: [C(11, 0, "alpha")]),
            M(2, anchor: "same", chunks: [C(21, 0, "alpha")]), M(3, chunks: [C(31, 0, "alpha")])], limit: -1))[0], (1, 11, 2));
    }

    private static void UnicodeTokenBoundaries()
    {
        var materials = new[]
        {
            M(1, chunks: [C(11, 0, "foo")]), M(2, chunks: [C(21, 0, "ifoo")]),
            M(3, title: "KK", chunks: [C(31, 0, "")]),
            M(4, chunks: [C(41, 0, "𝟙𝟚 １２ ١٢ 1.2 abc123 _x")]),
        };
        JsonElement[] replies = Exchange(Q("İFOO", materials), Q("KK"), Q("𝟙𝟚"), Q("𝟙"), Q("１２"), Q("١٢"),
            Q("1.2.3"), Q("\u001c ABC123\u0085_X\u001f"), Q("\U0001CCF0\U0001CCF1"));
        Matches(replies[0], (1, 11, 2), (2, 21, 2)); // Python dotted-I expands; the token is foo, not ifoo.
        Matches(replies[1], (3, 31, 2));
        Matches(replies[2], (4, 41, 2));
        Matches(replies[3]); // One supplementary digit is one code point, not a two-character term.
        Matches(replies[4], (4, 41, 2));
        Matches(replies[5], (4, 41, 2));
        Matches(replies[6], (4, 41, 2));
        Matches(replies[7], (4, 41, 4));
        Matches(replies[8]); // Outlined digits are Unicode16, not CPython3.13/Unicode15.1 decimal tokens.
    }

    private static void SnapshotTransitionsAndProtocolErrors()
    {
        var original = new[] { M(1, chunks: [C(11, 0, "alpha")]) };
        var edited = new[] { M(2, chunks: [C(21, 0, "beta")]) };
        string sentinel = "PRIVATE-TEXT-MUST-NOT-LEAK";
        JsonElement[] replies = Exchange("{\"query\":\"alpha\"}", Q("alpha", original, revision: "same"),
            Q("beta", edited, revision: "same"), Q("alpha"),
            Q("beta", [M(2, revoked: true, chunks: [C(21, 0, "beta")])], revision: "revoke"),
            Q("beta", edited, revision: "rollback"),
            Q("beta", [M(2, chunks: [C(21, 0, "beta")]), M(2, title: sentinel, chunks: [])], revision: "invalid"),
            Q("beta"), "{\"query\":\"" + sentinel + "\",", "{\"query\":null}", "[]",
            Q("beta", [], revision: "empty"), Q("beta"),
            Q("alpha", original, revision: "reset"), Q("alpha"),
            Q("", edited, revision: "zero", limit: 0), Q("beta"), "{\"query\":\"beta\"}");
        Error(replies[0], "no_snapshot");
        Matches(replies[1], (1, 11, 2));
        Matches(replies[2], (2, 21, 2));
        Equal(replies[2].GetProperty("revision").GetString(), "same");
        Matches(replies[3]);
        Matches(replies[4]);
        Matches(replies[5], (2, 21, 2));
        Error(replies[6], "invalid_snapshot");
        Matches(replies[7], (2, 21, 2));
        Equal(replies[7].GetProperty("revision").GetString(), "rollback");
        for (int i = 8; i <= 10; i++) Error(replies[i], "invalid_request");
        foreach (JsonElement reply in replies) Equal(reply.GetRawText().Contains(sentinel, StringComparison.Ordinal), false);
        Matches(replies[11]);
        Matches(replies[12]);
        Matches(replies[13], (1, 11, 2));
        Matches(replies[14], (1, 11, 2));
        Matches(replies[15]);
        Matches(replies[16], (2, 21, 2));
        Matches(replies[17], (2, 21, 2)); // Omitted limit defaults to eight.
        Equal(Exchange().Length, 0); // EOF exits cleanly and emits no extra protocol lines.
    }

    private static void DeterministicCorpusCases()
    {
        // Independent known-term oracle: vary chunk order, caps, ties, filters and
        // source collisions. This checks actual protocol outputs, not implementation text.
        var random = new Random(73421);
        for (int iteration = 0; iteration < 120; iteration++)
        {
            var materials = new List<Material>();
            for (int id = 1; id <= 18; id++)
            {
                var chunks = Enumerable.Range(0, random.Next(0, 5)).Select(index =>
                    C(id * 10 + index, index, Repeat("alpha", random.Next(0, 6)) + " " + Repeat("函数", random.Next(0, 6)))).Reverse().ToArray();
                materials.Add(M(id, course: random.Next(1, 4), verified: random.Next(2) == 1,
                    revoked: random.Next(7) == 0, anchor: random.Next(3) == 0 ? "source:" + id % 4 : "",
                    title: Repeat("alpha", random.Next(0, 6)), content: Repeat("函数", random.Next(0, 6)), chunks: chunks));
            }
            int? course = iteration % 3 == 0 ? 2 : null;
            int limit = iteration % 11 - 4;
            var expected = new List<(int MaterialId, int ChunkId, long Score, bool Verified, string Anchor)>();
            foreach (Material material in materials)
            {
                if (material.Revoked || (course.HasValue && material.CourseId != course.Value) || material.Chunks.Length == 0) continue;
                var best = material.Chunks.Select(chunk => (Chunk: chunk, Score: 2L *
                    (Math.Min(3, Count(material.Title, "alpha") + Count(chunk.Text, "alpha")) +
                     Math.Min(3, Count(material.Content, "函数") + Count(chunk.Text, "函数")))))
                    .OrderByDescending(value => value.Score).ThenBy(value => value.Chunk.ChunkIndex).ThenBy(value => value.Chunk.Id).First();
                if (best.Score > 0) expected.Add((material.Id, best.Chunk.Id, best.Score, material.Verified,
                    material.SourceAnchor.Length == 0 ? "material:" + material.Id : material.SourceAnchor));
            }
            var sources = new HashSet<string>(StringComparer.Ordinal);
            var distinct = expected.OrderByDescending(value => value.Score).ThenByDescending(value => value.Verified)
                .ThenBy(value => value.MaterialId).Where(value => sources.Add(value.Anchor)).ToList();
            int take = limit >= 0 ? Math.Min(limit, distinct.Count) : Math.Max(0, distinct.Count + limit);
            var expectedMatches = distinct.Take(take).Select(value => (value.MaterialId, value.ChunkId, value.Score)).ToArray();
            Matches(Exchange(Q("ALPHA 函数 alpha", materials.ToArray(), course: course, limit: limit))[0], expectedMatches);
        }
    }

    private static string Repeat(string term, int count) => string.Join(" ", Enumerable.Repeat(term, count));
    private static int Count(string text, string term) => text.Split(term, StringSplitOptions.None).Length - 1;

    private static Material M(int id, int course = 1, int? topic = null, int? document = null, string type = "definition",
        string title = "", string content = "", string[]? tags = null, string section = "", string anchor = "",
        bool verified = false, bool revoked = false, Chunk[]? chunks = null) =>
        new(id, course, topic, document, type, title, content, tags ?? [], section, anchor, verified, revoked, chunks ?? []);
    private static Chunk C(int id, int index, string text) => new(id, index, text);

    private static string Q(string query, Material[]? materials = null, string revision = "r1", int? course = null,
        int? topic = null, int? document = null, string[]? types = null, int[]? ids = null, int limit = 8) =>
        JsonSerializer.Serialize(new
        {
            snapshot = materials is null ? null : new { revision, materials }, query,
            course_id = course, topic_id = topic, source_document_id = document,
            material_types = types, material_ids = ids, limit,
        }, JsonOptions);

    private static JsonElement[] Exchange(params string[] requests)
    {
        using var input = new StringReader(string.Join("\n", requests));
        using var output = new MemoryStream();
        ProtocolHost.Run(input, output);
        string text = Encoding.UTF8.GetString(output.ToArray());
        string[] lines = text.Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Equal(lines.Length, requests.Length);
        return lines.Select(line =>
        {
            using JsonDocument document = JsonDocument.Parse(line);
            return document.RootElement.Clone();
        }).ToArray();
    }

    private static void Matches(JsonElement response, params (int MaterialId, int ChunkId, long Score)[] expected)
    {
        if (response.TryGetProperty("error", out _)) throw new InvalidOperationException("Unexpected protocol error: " + response);
        JsonElement.ArrayEnumerator values = response.GetProperty("matches").EnumerateArray();
        var actual = values.Select(value => (value.GetProperty("material_id").GetInt32(),
            value.GetProperty("chunk_id").GetInt32(), value.GetProperty("score").GetInt64())).ToArray();
        assertions++;
        if (!actual.SequenceEqual(expected))
            throw new InvalidOperationException($"Matches differ. Expected [{string.Join(", ", expected)}]; actual [{string.Join(", ", actual)}].");
    }

    private static void Error(JsonElement response, string code)
    {
        Equal(response.GetProperty("error").GetProperty("code").GetString(), code);
        Equal(response.GetProperty("error").GetProperty("message").GetString()!.Length < 100, true);
        Equal(response.TryGetProperty("matches", out _), false);
    }

    private static void Equal<T>(T actual, T expected)
    {
        assertions++;
        if (!EqualityComparer<T>.Default.Equals(actual, expected)) throw new InvalidOperationException($"Expected {expected}; actual {actual}.");
    }

    private sealed record Material(int Id, int CourseId, int? TopicId, int? SourceDocumentId, string MaterialType,
        string Title, string Content, string[] Tags, string Section, string SourceAnchor, bool Verified, bool Revoked, Chunk[] Chunks);
    private sealed record Chunk(int Id, int ChunkIndex, string Text);
}
