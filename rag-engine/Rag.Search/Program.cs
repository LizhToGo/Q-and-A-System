using System.Text;

namespace Rag.Search;

internal static class Program
{
    public static int Main()
    {
        Console.InputEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);
        try
        {
            using Stream output = Console.OpenStandardOutput();
            ProtocolHost.Run(Console.In, output);
            return 0;
        }
        catch (IOException)
        {
            // The parent owns process lifecycle; a closed pipe needs no diagnostics.
            return 1;
        }
    }
}
