@Test
public void verifierAccepts() {
  HttpRequest request = get("https://localhost");
  HttpsURLConnection connection = (HttpsURLConnection) request
      .getConnection();
  request.trustAllHosts();
  assertNotNull(connection.getHostnameVerifier());
  assertTrue(connection.getHostnameVerifier().verify(null, null));
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
  HttpRequest.setConnectionFactory(null); // Reset the factory to its default state after the test
}