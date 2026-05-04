  @Test
  public void putWithEscapedVarargsQueryParams() throws Exception {
    final Map<String, String> outputParams = new HashMap<String, String>();
    final AtomicReference<String> method = new AtomicReference<String>();
    handler = new RequestHandler() {

      @Override
      public void handle(Request request, HttpServletResponse response) {
        method.set(request.getMethod());
        outputParams.put("name", request.getParameter("name"));
        outputParams.put("number", request.getParameter("number"));
        response.setStatus(HTTP_OK);
      }
    };
    HttpRequest request = put(url, true, "name", "us er", "number", "100");
    assertTrue(request.ok());
    assertEquals("PUT", method.get());
    assertEquals("us er", outputParams.get("name"));
    assertEquals("100", outputParams.get("number"));
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

    ConnectionFactory originalFactory = HttpRequest.getConnectionFactory();
    try {
      HttpRequest.setConnectionFactory(factory);
      int code = get("http://not/a/real/url").code();
      assertEquals(200, code);
    } finally {
      HttpRequest.setConnectionFactory(originalFactory);
    }
  }