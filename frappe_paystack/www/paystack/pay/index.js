// process payment
let paynow = document.querySelector('#paynow').addEventListener(
  'click', e=>{
    e.preventDefault();
    // call api
    frappe.call({
            method: "frappe_paystack.frappe_paystack.doctype.paystack_settings.paystack_settings.get_payment_info", //dotted path to server method
            args: {data:e.target.value},
            callback: function(r) {
                // code snippet
                // console.log(r)
                if (r.message.status == 200){
                  // call flutter
                  result = r.message;
                  payWithPaystack(result.data);
                  // });
                } else {
                  frappe.throw("An error occurred with your payment")
                }
            }
    })
  }
)


// paystack
function payWithPaystack(data) {
  // e.preventDefault();
  console.log(data)
  let payload = data.payload;
  let href = `/orders/${payload.metadata.reference_docname}`
  let handler = PaystackPop.setup({
    // data.payload,
    key: payload.key, // Replace with your public key
    email: payload.email,
    amount: Number(payload.amount),
    ref: payload.ref, //''+Math.floor((Math.random() * 1000000000) + 1), // generates a pseudo-unique reference. Please replace with a reference you generated. Or remove the line entirely so our API will generate one for you
    currency: payload.currency,
    metadata:payload.metadata,
    // label: "Optional string that replaces customer email"
    onClose: function(){
      frappe.msgprint(__("You cancelled the payment."));
      window.location.href = href;
    },
    callback: function(response){
      console.log(response)
      let message = 'Payment complete! Reference: ' + response.reference;
      alert(message);
      frappe.msgprint(__("Your payment has been received and will be processed shortly."));
      setTimeout(function(){
        window.location.href = href;
        // alert("Hello");
      }, 6000);
    }
  });
  handler.openIframe();
}
