from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base
from pydantic import Field

mcp = FastMCP("DocumentMCP", log_level="ERROR")


docs = {
    "deposition.md": "This deposition covers the testimony of Angela Smith, P.E.",
    "report.pdf": "The report details the state of a 20m condenser tower.",
    "financials.docx": "These financials outline the project's budget and expenditures.",
    "outlook.pdf": "This document presents the projected future performance of the system.",
    "plan.md": "The plan outlines the steps for the project's implementation.",
    "spec.txt": "These specifications define the technical requirements for the equipment.",
}

@mcp.tool(
    name="read_doc_contents",
    description="Read the contents of a document and return it as a string.",
)
def read_document(
    doc_id: str = Field(description="Id of the document to read"),
) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with id {doc_id} not found")

    return docs[doc_id]


@mcp.tool(
    name="edit_document",
    description="Edit a document by replacing a string in the document's contents with a new string",
)
def edit_document(
    doc_id: str = Field(description="Id of the document that will be edited"),
    old_string: str = Field(description="The text to replace. Must match exactly, including whitespace."),
    new_string: str = Field(description="The new text to insert in place of the old text."),
) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with id {doc_id} not found")
    if old_string not in docs[doc_id]:
        raise ValueError(f"'{old_string}' not found in {doc_id}; nothing was changed")
    docs[doc_id] = docs[doc_id].replace(old_string, new_string)
    return f"Successfully edited {doc_id}"


@mcp.resource("docs://documents", mime_type="application/json")
def list_docs() -> list[str]:
    # MCP python sdk will automatically convert below into a string 
    return list(docs.keys())


@mcp.resource("docs://documents/{doc_id}", mime_type="text/plain")
def fetch_doc(doc_id: str) -> str:
    if doc_id not in docs:
        raise ValueError(f"Doc with id {doc_id} not found")
    return docs[doc_id]


@mcp.prompt(
    name="format",
    description="Rewrites the contents of the document in Markdown format."
)
def format_document(
    doc_id: str = Field(description="Id of the document to format")
) ->list[base.Message]:
    prompt = f"""
    Your goal is to reformat a document to be written with markdown syntax.

    The id of the document you need to reformat is:
    <document_id>
    {doc_id}
    </document_id>

    Add in headers, bullet points, tables, etc as necessary. Feel free to add in structure.
    Use the 'edit_document' tool to edit the document. After the document has been reformatted...
    """
    
    return [base.UserMessage(prompt)]

@mcp.prompt(
    name="summarize-a-doc",
    description="Summarize a doc in a particular way"
) 
def summarize_document(doc_id: str = Field(description="Id of the document to format")) ->list[base.Message]:
    prompt = f"""
    You goal is to summarize a document into a ONE liner.
    
     The id of the document you need to summarize is:
    <document_id>
    {doc_id}
    </document_id>

    The format should be like `Summarize of doc {doc_id} is: <the summarized content>`
    """
    return [base.UserMessage(prompt)]

if __name__ == "__main__":
    # Opt-in remote debugging: set DEBUG_MCP_SERVER=1 to make this stdio subprocess
    # wait for the VS Code "Attach to MCP server (:5679)" config before running.
    # debugpy uses a TCP socket, so it never corrupts the stdin/stdout JSON-RPC channel.
    import os

    if os.getenv("DEBUG_MCP_SERVER"):
        import debugpy

        debugpy.listen(("127.0.0.1", 5679))
        debugpy.wait_for_client()  # blocks until you attach; the client's connect() pauses here

    mcp.run(transport="stdio")

# To start the server inspector use:
# mcp dev mcp_server.py
