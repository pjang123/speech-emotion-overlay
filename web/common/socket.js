// Reconnecting WebSocket to the local backend. Messages are JSON objects with a `type`.
export function connect(path, { onMessage, onOpen = () => {}, onClose = () => {} }) {
  let delay = 500;
  const open = () => {
    const ws = new WebSocket(`ws://${location.host}${path}`);
    ws.onopen = () => {
      delay = 500;
      onOpen();
    };
    ws.onmessage = (event) => {
      try {
        onMessage(JSON.parse(event.data));
      } catch (err) {
        console.error("bad message", err);
      }
    };
    ws.onclose = () => {
      onClose();
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 5000);
    };
  };
  open();
}
