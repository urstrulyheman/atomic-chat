import React from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  Check,
  Coins,
  CreditCard,
  Gauge,
  LogOut,
  MessageCircle,
  Plus,
  RefreshCw,
  Search,
  Send,
  ShieldAlert,
  UserRound,
  Wallet
} from "lucide-react";
import "./styles.css";

const API = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";
const WS = API.replace(/^http/, "ws");
const TOKEN_KEY = "orca_access_token";

const money = (value) => Number(value || 0).toFixed(2);
const initials = (user) => (user?.name || user?.username || user?.phone || "?").slice(0, 2).toUpperCase();

async function request(path, options = {}) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API}${path}`, { ...options, headers });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error(data?.detail || "Request failed");
  return data;
}

function IconButton({ title, children, onClick, active, disabled }) {
  return (
    <button className={`icon-button ${active ? "active" : ""}`} title={title} aria-label={title} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
}

function Avatar({ user, size = "md" }) {
  return (
    <div className={`avatar ${size}`}>
      {user?.avatar_url ? <img src={user.avatar_url} alt="" /> : <span>{initials(user)}</span>}
    </div>
  );
}

function Login({ onAuthed }) {
  const [phone, setPhone] = React.useState("+919000000001");
  const [otp, setOtp] = React.useState("");
  const [name, setName] = React.useState("Himanshu");
  const [username, setUsername] = React.useState("himanshu");
  const [sent, setSent] = React.useState(false);
  const [devOtp, setDevOtp] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  async function sendOtp() {
    setBusy(true);
    setError("");
    try {
      const data = await request("/auth/send-otp", { method: "POST", body: JSON.stringify({ phone }) });
      setDevOtp(data.dev_otp);
      setOtp(data.dev_otp);
      setSent(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function verifyOtp() {
    setBusy(true);
    setError("");
    try {
      const data = await request("/auth/verify-otp", {
        method: "POST",
        body: JSON.stringify({ phone, otp, name, username })
      });
      sessionStorage.setItem(TOKEN_KEY, data.access_token);
      onAuthed(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-panel">
        <div className="brand-mark"><MessageCircle size={30} /><Coins size={20} /></div>
        <h1>Orca Chat Coin</h1>
        <p>Phone OTP, paid chat, wallet rewards, gas fees, and admin metrics in one working MVP loop.</p>
        <label>Phone number</label>
        <input value={phone} onChange={(e) => setPhone(e.target.value)} />
        {sent && (
          <>
            <label>Profile</label>
            <div className="split-input">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" />
              <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username" />
            </div>
            <label>OTP</label>
            <input value={otp} onChange={(e) => setOtp(e.target.value)} />
            <div className="hint">Dev OTP: {devOtp}</div>
          </>
        )}
        {error && <div className="error">{error}</div>}
        <button className="primary-action" onClick={sent ? verifyOtp : sendOtp} disabled={busy || !phone.trim()}>
          {busy ? "Working..." : sent ? "Verify and enter" : "Send OTP"}
        </button>
      </section>
      <section className="login-preview">
        <div className="metric-strip">
          <span><Wallet size={16} /> 20 ORCA welcome</span>
          <span><Gauge size={16} /> 0.25 gas/message</span>
          <span><ShieldAlert size={16} /> Fraud flags</span>
        </div>
      </section>
    </main>
  );
}

function Sidebar({ user, users, chats, activeChat, onStartChat, onSelectChat, onRefresh, onLogout }) {
  const [query, setQuery] = React.useState("");
  const filteredUsers = users.filter((u) => `${u.name} ${u.username} ${u.phone}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <Avatar user={user} />
        <div>
          <strong>{user.name || user.phone}</strong>
          <span>@{user.username || "profile"}</span>
        </div>
        <IconButton title="Refresh" onClick={onRefresh}><RefreshCw size={18} /></IconButton>
        <IconButton title="Logout" onClick={onLogout}><LogOut size={18} /></IconButton>
      </div>
      <div className="search-box">
        <Search size={16} />
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search people" />
      </div>
      <div className="section-label">Chats</div>
      <div className="list">
        {chats.map((chat) => {
          const other = users.find((u) => chat.members?.includes(u.id)) || chat.user;
          return (
            <button key={chat.id} className={`list-row ${activeChat?.id === chat.id ? "selected" : ""}`} onClick={() => onSelectChat(chat)}>
              <Avatar user={other} />
              <span><strong>{other?.name || "Conversation"}</strong><small>{chat.last || "Paid messages enabled"}</small></span>
            </button>
          );
        })}
      </div>
      <div className="section-label">Discover</div>
      <div className="list discover">
        {filteredUsers.length === 0 && (
          <button className="seed-button" onClick={onRefresh}>
            <Plus size={16} /> Seed demo contacts
          </button>
        )}
        {filteredUsers.map((person) => (
          <button key={person.id} className="list-row" onClick={() => onStartChat(person)}>
            <Avatar user={person} />
            <span><strong>{person.name || person.phone}</strong><small>{person.phone}</small></span>
            <Plus size={16} />
          </button>
        ))}
      </div>
    </aside>
  );
}

function WalletPanel({ wallet, payments, transactions, onRecharge }) {
  return (
    <aside className="right-panel">
      <div className="wallet-hero">
        <span>Spendable</span>
        <strong>{money(wallet?.spendable_balance)} ORCA</strong>
        <small>{money(wallet?.locked_balance)} locked rewards</small>
      </div>
      <div className="balance-grid">
        <div><span>Purchased</span><strong>{money(wallet?.purchased_balance)}</strong></div>
        <div><span>Earned</span><strong>{money(wallet?.earned_balance)}</strong></div>
        <div><span>Gas paid</span><strong>{money(wallet?.gas_paid_total)}</strong></div>
        <div><span>Rewards</span><strong>{money(wallet?.reward_earned_total)}</strong></div>
      </div>
      <button className="primary-action compact" onClick={onRecharge}><CreditCard size={17} /> Recharge ₹99</button>
      <div className="section-label">Transactions</div>
      <div className="tx-list">
        {transactions.slice(0, 8).map((tx) => (
          <div key={tx.id} className="tx-row">
            <span className="tx-icon">{tx.transaction_type === "message_send" ? <Send size={15} /> : <Coins size={15} />}</span>
            <div><strong>{tx.transaction_type.replaceAll("_", " ")}</strong><small>Gas {money(tx.platform_gas)} · Reward {money(tx.receiver_reward)}</small></div>
            <b>{money(tx.gross_amount)}</b>
          </div>
        ))}
        {transactions.length === 0 && <div className="empty-small">No ledger entries yet</div>}
      </div>
      <div className="section-label">Payments</div>
      <div className="tx-list">
        {payments.slice(0, 4).map((pay) => (
          <div key={pay.id} className="tx-row">
            <span className="tx-icon"><CreditCard size={15} /></span>
            <div><strong>₹{money(pay.amount)}</strong><small>{pay.status} · {money(pay.coins_to_credit)} ORCA</small></div>
          </div>
        ))}
      </div>
    </aside>
  );
}

function ChatView({ chat, messages, currentUser, onSend, wsOnline }) {
  const [text, setText] = React.useState("");
  const endRef = React.useRef(null);
  React.useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), [messages, chat?.id]);
  if (!chat) {
    return (
      <section className="chat-empty">
        <MessageCircle size={74} />
        <h2>Select a chat</h2>
        <p>Start with a discovered user, send a paid message, then watch balances and gas update live.</p>
      </section>
    );
  }
  const other = chat.user;
  async function submit() {
    const value = text.trim();
    if (!value) return;
    setText("");
    await onSend(value);
  }
  return (
    <section className="chat-pane">
      <header className="chat-header">
        <Avatar user={other} />
        <div><strong>{other?.name || other?.phone}</strong><span>{wsOnline ? "WebSocket online" : "REST fallback ready"}</span></div>
        <div className="fee-pill">1 ORCA/message</div>
      </header>
      <div className="message-wall">
        {messages.map((msg) => {
          const mine = msg.sender_id === currentUser.id;
          return (
            <div key={msg.id} className={`bubble-row ${mine ? "mine" : ""}`}>
              <div className="bubble">
                <p>{msg.content}</p>
                <span>{mine ? <ArrowUpRight size={12} /> : <ArrowDownLeft size={12} />} {money(msg.coin_cost)} ORCA · {msg.status}</span>
              </div>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
      <footer className="composer">
        <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="Type a paid message..." onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }} />
        <IconButton title="Send paid message" onClick={submit} disabled={!text.trim()}><Send size={20} /></IconButton>
      </footer>
    </section>
  );
}

function AdminBar({ metrics }) {
  return (
    <div className="admin-bar">
      <span><UserRound size={15} /> {metrics?.users?.total ?? 0} users</span>
      <span><MessageCircle size={15} /> {metrics?.chat?.total_messages ?? 0} messages</span>
      <span><Coins size={15} /> {money(metrics?.wallet?.total_gas_collected)} gas</span>
      <span><ShieldAlert size={15} /> {metrics?.fraud?.open_events ?? 0} fraud flags</span>
      <span><Activity size={15} /> ₹{money(metrics?.payments?.recharge_revenue)}</span>
    </div>
  );
}

function AppShell({ initialUser, onLogout }) {
  const [user, setUser] = React.useState(initialUser);
  const [users, setUsers] = React.useState([]);
  const [chats, setChats] = React.useState([]);
  const [activeChat, setActiveChat] = React.useState(null);
  const [messages, setMessages] = React.useState({});
  const [wallet, setWallet] = React.useState(null);
  const [transactions, setTransactions] = React.useState([]);
  const [payments, setPayments] = React.useState([]);
  const [metrics, setMetrics] = React.useState(null);
  const [wsOnline, setWsOnline] = React.useState(false);
  const [notice, setNotice] = React.useState("");

  const load = React.useCallback(async () => {
    const [me, people, walletData, txData, payData] = await Promise.all([
      request("/auth/me"),
      request("/users"),
      request("/wallet/balance"),
      request("/wallet/transactions"),
      request("/payments/history")
    ]);
    const metricData = await request("/admin/metrics").catch(() => null);
    setUser(me);
    setUsers(people);
    setWallet(walletData);
    setTransactions(txData);
    setPayments(payData);
    setMetrics(metricData);
  }, []);

  async function seedAndLoad() {
    if (users.length === 0) {
      await request("/users/dev-seed", { method: "POST", body: JSON.stringify({}) });
    }
    await load();
  }

  React.useEffect(() => {
    load().catch((err) => setNotice(err.message));
  }, [load]);

  React.useEffect(() => {
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (!token) return undefined;
    const socket = new WebSocket(`${WS}/ws/chat?token=${encodeURIComponent(token)}`);
    socket.onopen = () => setWsOnline(true);
    socket.onclose = () => setWsOnline(false);
    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "message.received") {
        const msg = data.payload;
        setMessages((prev) => ({ ...prev, [msg.conversation_id]: [...(prev[msg.conversation_id] || []), msg] }));
        load().catch(() => {});
      }
    };
    return () => socket.close();
  }, [load]);

  async function startChat(person) {
    const chat = await request("/chats", { method: "POST", body: JSON.stringify({ receiver_id: person.id }) });
    const enriched = { ...chat, user: person, members: [user.id, person.id] };
    setChats((prev) => [enriched, ...prev.filter((item) => item.id !== chat.id)]);
    setActiveChat(enriched);
    const rows = await request(`/chats/${chat.id}/messages`);
    setMessages((prev) => ({ ...prev, [chat.id]: rows }));
  }

  async function selectChat(chat) {
    setActiveChat(chat);
    const rows = await request(`/chats/${chat.id}/messages`);
    setMessages((prev) => ({ ...prev, [chat.id]: rows }));
  }

  async function sendMessage(content) {
    const msg = await request(`/chats/${activeChat.id}/messages`, {
      method: "POST",
      body: JSON.stringify({ receiver_id: activeChat.user.id, content })
    });
    setMessages((prev) => ({ ...prev, [activeChat.id]: [...(prev[activeChat.id] || []), msg] }));
    await load();
  }

  async function recharge() {
    const order = await request("/payments/razorpay/order", { method: "POST", body: JSON.stringify({ pack_id: "starter_99" }) });
    await request("/payments/dev/capture", { method: "POST", body: JSON.stringify({ gateway_order_id: order.gateway_order_id }) });
    setNotice("₹99 demo recharge captured. 100 ORCA credited.");
    await load();
  }

  function logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    onLogout();
  }

  return (
    <main className="app-shell">
      <Sidebar user={user} users={users} chats={chats} activeChat={activeChat} onStartChat={startChat} onSelectChat={selectChat} onRefresh={seedAndLoad} onLogout={logout} />
      <div className="center-stack">
        <AdminBar metrics={metrics} />
        {notice && <button className="notice" onClick={() => setNotice("")}>{notice}<Check size={16} /></button>}
        <ChatView chat={activeChat} messages={messages[activeChat?.id] || []} currentUser={user} onSend={sendMessage} wsOnline={wsOnline} />
      </div>
      <WalletPanel wallet={wallet} transactions={transactions} payments={payments} onRecharge={recharge} />
    </main>
  );
}

function App() {
  const [user, setUser] = React.useState(null);
  const [checking, setChecking] = React.useState(Boolean(sessionStorage.getItem(TOKEN_KEY)));

  React.useEffect(() => {
    if (!sessionStorage.getItem(TOKEN_KEY)) return;
    request("/auth/me")
      .then(setUser)
      .catch(() => sessionStorage.removeItem(TOKEN_KEY))
      .finally(() => setChecking(false));
  }, []);

  if (checking) return <div className="boot">Loading Orca Chat Coin...</div>;
  if (!user) return <Login onAuthed={setUser} />;
  return <AppShell initialUser={user} onLogout={() => setUser(null)} />;
}

createRoot(document.getElementById("root")).render(<App />);
