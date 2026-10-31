using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

static class Program
{
    static string? Id(ISymbol? symbol)
    {
        if (symbol is null || symbol is ITypeSymbol { TypeKind: TypeKind.Error }) return null;
        symbol = symbol.OriginalDefinition;
        var doc = symbol.GetDocumentationCommentId();
        return doc is null ? null : $"{symbol.ContainingAssembly?.Name ?? "namespace"}::{doc}";
    }

    static object Location(SyntaxNode node)
    {
        var span = node.GetLocation().GetLineSpan();
        return new { path = node.SyntaxTree.FilePath, start_line = span.StartLinePosition.Line + 1,
            end_line = span.EndLinePosition.Line + 1, start_column = span.StartLinePosition.Character + 1,
            end_column = span.EndLinePosition.Character + 1, start = node.SpanStart, length = node.Span.Length };
    }

    static bool Declaration(SyntaxNode node) =>
        node is BaseNamespaceDeclarationSyntax or BaseTypeDeclarationSyntax or DelegateDeclarationSyntax
            or BaseMethodDeclarationSyntax or BasePropertyDeclarationSyntax or EnumMemberDeclarationSyntax
        || node is VariableDeclaratorSyntax { Parent.Parent: BaseFieldDeclarationSyntax };

    static string Signature(SyntaxNode node)
    {
        var end = node switch {
            BaseMethodDeclarationSyntax m => m.Body?.SpanStart ?? m.ExpressionBody?.SpanStart ?? m.Span.End,
            BaseTypeDeclarationSyntax t => t.OpenBraceToken.SpanStart,
            BasePropertyDeclarationSyntax p => p.AccessorList?.SpanStart ?? p.Span.End,
            BaseNamespaceDeclarationSyntax n => n.Name.Span.End,
            _ => node.Span.End
        };
        return node.SyntaxTree.GetText().ToString(Microsoft.CodeAnalysis.Text.TextSpan.FromBounds(node.SpanStart, Math.Max(node.SpanStart, end))).Trim();
    }

    static string UnityType(INamedTypeSymbol? type)
    {
        var seen = new HashSet<ISymbol>(SymbolEqualityComparer.Default);
        while (type is not null && seen.Add(type))
        {
            if (type.TypeKind == TypeKind.Error) return "unresolved";
            var name = type.ToDisplayString();
            if (name is "UnityEngine.MonoBehaviour" or "UnityEngine.ScriptableObject") return name;
            type = type.BaseType;
        }
        return "none";
    }

    public static int Main()
    {
        try
        {
            using var input = JsonDocument.Parse(Console.In.ReadToEnd());
            var data = input.RootElement;
            var defines = data.GetProperty("defines").EnumerateArray().Select(x => x.GetString()!).ToArray();
            if (!LanguageVersionFacts.TryParse(data.GetProperty("language_version").GetString(), out var language))
                throw new ArgumentException("Unsupported C# language version.");
            var options = new CSharpParseOptions(language, DocumentationMode.Parse, preprocessorSymbols: defines);
            var references = ((string?)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") ?? "")
                .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
                .Concat(data.GetProperty("references").EnumerateArray().Select(x => x.GetString()!))
                .Distinct().Select(p => MetadataReference.CreateFromFile(p)).ToArray();
            var files = data.GetProperty("files").EnumerateArray().ToArray();
            var declarations = new List<object>();
            var edges = new List<object>();
            var diagnostics = new List<object>();
            var conditions = new List<object>();
            foreach (var group in files.GroupBy(f => f.GetProperty("assembly").GetString()!))
            {
                var trees = group.Select(f => CSharpSyntaxTree.ParseText(
                    f.GetProperty("text").GetString()!, options, f.GetProperty("path").GetString()!)).ToArray();
                var compilation = CSharpCompilation.Create(group.Key, trees, references,
                    new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary, allowUnsafe: true));
                var errors = compilation.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToArray();
                foreach (var diagnostic in compilation.GetDiagnostics())
                {
                    var line = diagnostic.Location.GetLineSpan();
                    diagnostics.Add(new { assembly = group.Key, code = diagnostic.Id,
                        severity = diagnostic.Severity.ToString(), message = diagnostic.GetMessage(),
                        path = diagnostic.Location.IsInSource ? line.Path : null,
                        line = diagnostic.Location.IsInSource ? line.StartLinePosition.Line + 1 : (int?)null });
                }
                foreach (var tree in trees)
                {
                    var root = tree.GetRoot();
                    var model = compilation.GetSemanticModel(tree);
                    foreach (var trivia in root.DescendantTrivia(descendIntoTrivia: true))
                    {
                        if (trivia.IsDirective || trivia.IsKind(SyntaxKind.DisabledTextTrivia))
                            conditions.Add(new { path = tree.FilePath, text = trivia.ToFullString(),
                                kind = trivia.Kind().ToString(),
                                line = tree.GetLineSpan(trivia.Span).StartLinePosition.Line + 1 });
                    }
                    foreach (var node in root.DescendantNodes().Where(Declaration))
                    {
                        var symbol = model.GetDeclaredSymbol(node);
                        var owner = node is VariableDeclaratorSyntax ? node.Parent!.Parent! : node;
                        var attributes = owner.ChildNodes().OfType<AttributeListSyntax>().SelectMany(a => a.Attributes)
                            .Select(a => a.ToString()).ToArray();
                        var field = symbol as IFieldSymbol;
                        var attrs = field?.GetAttributes();
                        var hasSerialize = attrs?.Any(a => a.AttributeClass?.TypeKind != TypeKind.Error &&
                            a.AttributeClass?.ToDisplayString() is "UnityEngine.SerializeField" or "UnityEngine.SerializeReference") ?? false;
                        var nonSerialized = attrs?.Any(a => a.AttributeClass?.ToDisplayString() == "System.NonSerializedAttribute") ?? false;
                        var serialized = field is null ? "not_a_field" :
                            field.IsStatic || field.IsConst || field.IsReadOnly || nonSerialized ? "excluded" :
                            hasSerialize || field.DeclaredAccessibility == Accessibility.Public ? "candidate_type_rules_not_checked" :
                            attributes.Any(a => a.Contains("SerializeField") || a.Contains("SerializeReference")) ?
                                "unresolved_attribute_candidate" : "none";
                        declarations.Add(new { id = Id(symbol) ?? $"{group.Key}::{tree.FilePath}:{node.SpanStart}",
                            assembly = group.Key, kind = node.Kind().ToString(), name = symbol?.Name ?? node.ToString(),
                            qualified_name = symbol?.ToDisplayString(), container = Id(symbol?.ContainingSymbol),
                            signature = Signature(owner), location = Location(node), attributes,
                            modifiers = owner.ChildTokens().Where(t => SyntaxFacts.IsKeywordKind(t.Kind())).Select(t => t.Text).ToArray(),
                            comments = owner.GetLeadingTrivia().ToFullString(),
                            bases = (node as BaseTypeDeclarationSyntax)?.BaseList?.Types.Select(b => b.Type.ToString()).ToArray() ?? Array.Empty<string>(),
                            unity_type = UnityType(symbol as INamedTypeSymbol), serialized_field = serialized,
                            status = symbol is null ? "syntax_only" : "declared",
                            partial = owner.ChildTokens().Any(t => t.IsKind(SyntaxKind.PartialKeyword)) });
                    }
                    foreach (var node in root.DescendantNodes().Where(n =>
                        n is InvocationExpressionSyntax or ObjectCreationExpressionSyntax or SimpleNameSyntax or SimpleBaseTypeSyntax))
                    {
                        var expression = node is SimpleBaseTypeSyntax b ? b.Type : node;
                        var info = model.GetSymbolInfo(expression);
                        var target = Id(info.Symbol);
                        var overlappingError = errors.Any(d => d.Location.SourceTree == tree && d.Location.SourceSpan.IntersectsWith(node.Span));
                        var resolution = target is not null && !overlappingError ? "resolved" :
                            info.CandidateSymbols.Length > 0 ? "ambiguous_or_invalid" : "unresolved";
                        var enclosing = model.GetEnclosingSymbol(node.SpanStart);
                        if (node is SimpleBaseTypeSyntax)
                        {
                            var declaringType = node.Ancestors().OfType<BaseTypeDeclarationSyntax>().FirstOrDefault();
                            if (declaringType is not null) enclosing = model.GetDeclaredSymbol(declaringType);
                        }
                        edges.Add(new { kind = node is InvocationExpressionSyntax ? "calls" :
                            node is ObjectCreationExpressionSyntax ? "constructs" :
                            node is SimpleBaseTypeSyntax ? "inherits_or_implements" : "references",
                            source = Id(enclosing), target = resolution == "resolved" ? target : null,
                            target_display = resolution == "resolved" ? info.Symbol?.ToDisplayString() : null,
                            text = node.ToString(), location = Location(node), resolution,
                            candidate_reason = info.CandidateReason.ToString(),
                            candidates = info.CandidateSymbols.Select(Id).Where(id => id is not null).ToArray() });
                    }
                }
            }
            Console.Write(JsonSerializer.Serialize(new { schema_version = 1,
                roslyn_version = typeof(CSharpCompilation).Assembly.GetName().Version?.ToString(),
                declarations, edges, diagnostics, conditions }));
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            return 1;
        }
    }
}
