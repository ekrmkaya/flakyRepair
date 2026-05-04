@Test
public void basicProxyAuthentication() throws Exception {
  final AtomicBoolean finalHostReached = new AtomicBoolean(false);
  handler = new RequestHandler() {

    @Override
    public void handle(Request request, HttpServletResponse response) {
      finalHostReached.set(true);
      response.setStatus(HTTP_OK);
    }
  };
  HttpRequest.setConnectionFactory(HttpRequest.DEFAULT_CONNECTION_FACTORY);
  assertTrue(get(url).useProxy("localhost", proxyPort).proxyBasic("user", "p4ssw0rd").ok());
  assertEquals("user", proxyUser.get());
  assertEquals("p4ssw0rd", proxyPassword.get());
  assertEquals(true, finalHostReached.get());
  assertEquals(1, proxyHitCount.get());
}

@Test
public void customConnectionFactory() throws Exception {
  handler = new RequestHandler() {

    @Override
    public void handle(Request request, HttpServletResponse response) {
      response.setStatus(HTTP_OK);
    }
  };

  ConnectionFactory factory = new ConnectionFactory() {

    public HttpURLConnection create(URL otherUrl) throws IOException {
      return (HttpURLConnection) new URL(url).openConnection();
    }

    public HttpURLConnection create(URL url, Proxy proxy) throws IOException {
      throw new IOException();
    }
  };

  HttpRequest.setConnectionFactory(factory);
  int code = get("http://not/a/real/url").code();
  assertEquals(200, code);
  HttpRequest.setConnectionFactory(HttpRequest.DEFAULT_CONNECTION_FACTORY);
}