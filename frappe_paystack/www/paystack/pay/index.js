// process payment
document.addEventListener("DOMContentLoaded", function(e) {
//do work
let scrtag = document.createElement('script');
scrtag.src = "https://js.paystack.co/v1/inline.js"
scrtag.type = "text/javascript";
document.head.appendChild(scrtag);
// launch pay screen
let value = document.querySelector('#paynow').value;
preparedata(value);
})


let paynow = document.querySelector('#paynow').addEventListener(
  'click', e=>{
    e.preventDefault();
    // call api
    preparedata(e.target.value);

  }
)

let preparedata = (value)=>{
  frappe.call({
          method: "frappe_paystack.frappe_paystack.doctype.paystack_settings.paystack_settings.get_payment_info", //dotted path to server method
          args: {data:value},
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
      // complete payment
      frappe.call({
          method: "frappe_paystack.www.paystack.pay.webhook.make_doc", //dotted path to server method
          args: response,
          callback: function(r) {
              // code snippet
              // console.log(r);
          }
      })
      let message = 'Payment complete! Reference: ' + response.reference;
      // alert(message);
      frappe.msgprint({
          title: __('Notification'),
          indicator: 'green',
          message: __('Your payment has been received and will be processed shortly.')
      });

      setTimeout(function(){
        window.location.href = href;
        // alert("Hello");
      }, 10000);
    }
  });
  handler.openIframe();
}



// frappe.call({
//           method: "frappe_paystack.www.paystack.pay.webhook.make_doc", //dotted path to server method
//           args: {
//               message: "Approved",
//               reference: "69e86bcb56",
//               status: "success",
//               trans: "1354472780",
//               transaction: "1354472780",
//               trxref: "69e86bcb56"},
//           callback: function(r) {
//               // code snippet
//               console.log(r);
//           }
//       })
