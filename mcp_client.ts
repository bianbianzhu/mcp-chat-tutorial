import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { URL } from "url";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// Protocol client that maintain 1:1 connection with servers
class MCPClient {
  public get client() {
    return this.#client;
  }

  #client: Client;
  #transportType: "stdio" | "httpStream";
  #transport: StreamableHTTPClientTransport | StdioClientTransport | null =
    null;
  #isCompleted: boolean = false;

  constructor({ transportType }: { transportType: "stdio" | "httpStream" }) {
    this.#client = new Client({
      name: `mcp-client`,
      version: "1.0.0",
    });
    this.#transportType = transportType;
  }

  async connectToServer(serverUrl?: string) {
    if (this.#transportType === "httpStream") {
      if (!serverUrl) {
        throw new Error("serverUrl is required for httpStream transport");
      }
      const url = new URL(serverUrl);
      try {
        // uuid for the transport will be auto generated
        this.#transport = new StreamableHTTPClientTransport(url, {
          requestInit: {
            headers: {
              ["x-api-key"]: `Bearer 1234567890`,
            },
          },
        });
        await this.#client.connect(this.#transport, {
          timeout: 10000000,
        });
        console.log(
          `Connected to server via httpStream transport; transport session id: ${this.#transport.sessionId}`
        );

        this.#setUpTransport();
      } catch (e) {
        console.log("Failed to connect to MCP server via httpStream: ", e);
        throw e;
      }
    } else {
      try {
        // this.#transport = new StdioClientTransport({
        //   command: "npx",
        //   args: ["tsx", "src/index.ts"],
        // });

        // TODO: make the command and args configurable
        this.#transport = new StdioClientTransport({
          command: "npx",
          args: ["tsx", "src/mcp-servers/raw-stdio-server-quick-start.ts"],
        });

        await this.#client.connect(this.#transport);

        this.#setUpTransport();
      } catch (e) {
        console.log("Failed to connect to MCP server via stdio: ", e);
        throw e;
      }
    }
  }

  #setUpTransport() {
    if (this.#transport === null) {
      return;
    }
    this.#transport.onclose = () => {
      console.log("transport closed.");
      this.#isCompleted = true;
    };

    this.#transport.onerror = async (error) => {
      console.log("transport error: ", error);
      await this.cleanup();
    };

    // 🚨 Below will stop the connection between each request and response
    // this.#transport.onmessage = (message) => {
    //   console.log("message received: ", message);
    // };
  }

  async waitForCompletion() {
    while (!this.#isCompleted) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }

  async cleanup() {
    await this.#client.close();
  }
}

async function multiHttpStreamClients() {
  const port = process.env.PORT ?? 8080;
  const client1 = new MCPClient({ transportType: "httpStream" });
  //   const client2 = new MCPClient({ transportType: "httpStream" });

  try {
    await client1.connectToServer(`http://localhost:${port}/mcp`);
    // await client2.connectToServer(`http://localhost:${port}/mcp`);

    const result1 = await client1.client.listTools();
    console.log(result1);

    // const result2 = await client2.client.listTools();
    // console.log(result2);

    const toolResult1 = await client1.client.callTool({
      name: "add",
      arguments: {
        a: 1,
        b: 3,
      },
    });
    console.log("callTool: ", JSON.stringify(toolResult1, null, 2));
  } finally {
    // await client1.cleanup();
    // await client2.cleanup();
  }
}

multiHttpStreamClients();

async function multiStdioClients() {
  const client1 = new MCPClient({ transportType: "stdio" });
  // const client2 = new MCPClient({ transportType: "stdio" });

  try {
    await client1.connectToServer();
    // await client2.connectToServer();

    const result1 = await client1.client.listTools();
    console.log("listTools: ", JSON.stringify(result1, null, 2));

    const toolResult1 = await client1.client.callTool({
      name: "add",
      arguments: {
        a: 1,
        b: 3,
      },
    });
    console.log("callTool: ", JSON.stringify(toolResult1, null, 2));

    // const result2 = await client2.client.listTools();
    // console.log(result2);
  } finally {
    // await client1.cleanup();
    // await client2.cleanup();
  }
}

// multiStdioClients();